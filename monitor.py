# -*- coding: utf-8 -*-
"""
财经信息监控系统 - 主程序
流程: 拉取博主最新推文 -> 去重 -> 关键词粗筛 -> AI精筛+翻译 -> 微信推送(PushPlus)

设计原则:
  1. 数据源可插拔 (mock / rsshub / twitterapi_io)，在 config.yaml 里一行切换
  2. 所有密钥从环境变量读取(GitHub Secrets)，代码和配置文件里不含任何秘密
  3. AI 挂了不影响推送(fail-open)：照常推送但标注「未评级」
"""

import html
import hashlib
import json
import os
import re
import secrets
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, date, timezone, timedelta
from email.utils import parsedate_to_datetime

import requests
import yaml

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.yaml")
STATE_PATH = os.path.join(BASE_DIR, "state.json")
SOURCES_CFG_PATH = os.path.join(BASE_DIR, "sources.yaml")
DATA_DIR = os.path.join(BASE_DIR, "data")
SITE_DATA_DIR = os.path.join(BASE_DIR, "site", "data")
EVENTS_PATH = os.path.join(DATA_DIR, "events.json")        # 事件流（网站展示）
CALENDAR_PATH = os.path.join(DATA_DIR, "calendar.json")    # 未来30天财经日历
MARKET_PATH = os.path.join(DATA_DIR, "market.json")        # 行情+情绪读数快照

CST = timezone(timedelta(hours=8))  # 北京时间

_HTTP_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; finance-monitor/1.0)"}

# ------------------------------------------------------------
# 工具函数
# ------------------------------------------------------------

def log(msg):
    print(f"[{datetime.now(CST):%H:%M:%S}] {msg}", flush=True)


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


def secret(env_name, cfg_value=""):
    """环境变量优先（云端走 Secrets），其次配置文件（本地测试用）"""
    return os.environ.get(env_name) or (cfg_value or "")


def load_sources_cfg():
    if os.path.exists(SOURCES_CFG_PATH):
        with open(SOURCES_CFG_PATH, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {}


def _write_json(path, obj):
    """同时写入 data/（仓库留存）和 site/data/（网站部署产物）"""
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(SITE_DATA_DIR, exist_ok=True)
    body = json.dumps(obj, ensure_ascii=False, indent=1)
    for p in (path, os.path.join(SITE_DATA_DIR, os.path.basename(path))):
        with open(p, "w", encoding="utf-8") as f:
            f.write(body)


def load_events():
    if os.path.exists(EVENTS_PATH):
        with open(EVENTS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def save_events(events, max_days=90, max_count=800):
    """滚动保留：最多 max_days 天、max_count 条"""
    cutoff = (datetime.now(CST) - timedelta(days=max_days)).isoformat()
    events = [e for e in events if (e.get("created_at") or "") >= cutoff]
    events.sort(key=lambda e: e.get("created_at") or "", reverse=True)
    _write_json(EVENTS_PATH, events[:max_count])


# ------------------------------------------------------------
# 数据源：每个 provider 返回标准化推文列表
# 标准字段: {id, handle, author, text, created_at(datetime|None), url}
# ------------------------------------------------------------

def fetch_mock(handle, cfg):
    """读取本地样例数据，按 handle 过滤 —— 本地调试用"""
    path = os.path.join(BASE_DIR, "samples", "sample_tweets.json")
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        rows = json.load(f)
    out = []
    for r in rows:
        if r.get("handle") == handle:
            out.append({
                "id": str(r["id"]),
                "handle": r["handle"],
                "author": r.get("author", handle),
                "text": r.get("text", ""),
                "created_at": None,
                "url": r.get("url") or f"https://x.com/{handle}/status/{r['id']}",
            })
    return out


_STATUS_ID_RE = re.compile(r"/status/(\d+)")


def fetch_rsshub(handle, cfg):
    """通过自建 RSSHub 的 /twitter/user/:id 路由拉取（免费方案）"""
    base = secret("RSSHUB_BASE", cfg.get("rsshub_base", "")).rstrip("/")
    if not base:
        raise RuntimeError("RSSHUB_BASE 未配置")
    resp = requests.get(
        f"{base}/twitter/user/{handle}",
        timeout=25,
        headers={"User-Agent": "Mozilla/5.0"},
    )
    resp.raise_for_status()
    root = ET.fromstring(resp.content)
    out = []
    for item in root.iter("item"):
        link = (item.findtext("link") or "").strip()
        m = _STATUS_ID_RE.search(link)
        if not m:
            continue
        desc_html = item.findtext("description") or ""
        text = re.sub(r"<[^>]+>", " ", desc_html)
        text = html.unescape(re.sub(r"\s+", " ", text)).strip()
        created = None
        pub = item.findtext("pubDate")
        if pub:
            try:
                created = parsedate_to_datetime(pub)
            except Exception:
                pass
        out.append({
            "id": m.group(1),
            "handle": handle,
            "author": item.findtext("author") or item.findtext("{*}creator") or handle,
            "text": text,
            "created_at": created,
            "url": link,
        })
    return out


def fetch_twitterapi_io(handle, cfg):
    """twitterapi.io 按量付费接口（免费方案失效时的升级路线）"""
    key = secret("TWITTERAPI_IO_KEY", cfg.get("twitterapi_io_key", ""))
    if not key:
        raise RuntimeError("TWITTERAPI_IO_KEY 未配置")
    resp = requests.get(
        "https://api.twitterapi.io/twitter/user/last_tweets",
        params={"userName": handle},
        headers={"X-API-Key": key},
        timeout=25,
    )
    resp.raise_for_status()
    out = []
    for t in resp.json().get("tweets", []):
        tid = str(t.get("id") or "")
        if not tid:
            continue
        created = None
        ca = t.get("createdAt")
        if ca:
            try:  # 形如 Wed Oct 10 08:00:00 +0000 2024
                created = datetime.strptime(ca, "%a %b %d %H:%M:%S %z %Y")
            except Exception:
                pass
        a = t.get("author") or {}
        out.append({
            "id": tid,
            "handle": handle,
            "author": a.get("name") or handle,
            "text": t.get("text") or "",
            "created_at": created,
            "url": f"https://x.com/{handle}/status/{tid}",
        })
    return out


PROVIDERS = {
    "mock": fetch_mock,
    "rsshub": fetch_rsshub,
    "twitterapi_io": fetch_twitterapi_io,
    "x_graphql": None,  # 占位，函数定义在下方注册
}


# ------------------------------------------------------------
# 数据源：直连 X 官方接口（免费，用 auth_token 登录凭证）
# 指纹(queryId)动态获取自社区维护文档，失效时用兜底值
# ------------------------------------------------------------

_X_BEARER = ("AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs"
             "%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA")
_X_FALLBACK_QIDS = {
    "UserByScreenName": "Gb-d6r0vxPOADdG62OEBpQ",
    "UserTweets": "eoJ5zbv51Z_KVl81v9PmLQ",
}
_X_FEATURES_USER = {
    "hidden_profile_subscriptions_enabled": True, "profile_label_stickers_enabled": False,
    "rweb_tipjar_consumption_enabled": True,
    "responsive_web_graphql_exclude_directive_enabled": True,
    "verified_phone_label_enabled": False,
    "subscriptions_verification_info_is_identity_verified_enabled": True,
    "subscriptions_verification_info_verified_since_enabled": True,
    "highlights_tweets_tab_ui_enabled": True,
    "responsive_web_twitter_article_notes_tab_enabled": True,
    "subscriptions_feature_can_gift_premium": True,
    "creator_subscriptions_tweet_preview_api_enabled": True,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "responsive_web_graphql_timeline_navigation_enabled": True,
}
_X_FEATURES_FEED = {
    "rweb_tipjar_consumption_enabled": True,
    "responsive_web_graphql_exclude_directive_enabled": True,
    "verified_phone_label_enabled": False,
    "creator_subscriptions_tweet_preview_api_enabled": True,
    "responsive_web_graphql_timeline_navigation_enabled": True,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "communities_web_enable_tweet_community_results_fetch": True,
    "c9s_tweet_anatomy_moderator_badge_enabled": True,
    "articles_preview_enabled": True,
    "responsive_web_edit_tweet_api_enabled": True,
    "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
    "view_counts_everywhere_api_enabled": True,
    "longform_notetweets_consumption_enabled": True,
    "responsive_web_twitter_article_tweet_consumption_enabled": True,
    "tweet_awards_web_tipping_enabled": False,
    "creator_subscriptions_quote_tweet_preview_enabled": False,
    "freedom_of_speech_not_reach_fetch_enabled": True,
    "standardized_nudges_misinfo": True,
    "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
    "rweb_video_timestamps_enabled": True,
    "longform_notetweets_rich_text_read_enabled": True,
    "longform_notetweets_inline_media_enabled": True,
    "responsive_web_enhance_cards_enabled": False,
}

_UID_CACHE = {}  # handle -> rest_id，进程内缓存避免重复解析


def _x_headers(auth):
    ct0 = secrets.token_hex(16)  # X 只要求 csrf cookie 与 header 一致，可自生成
    return {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "Authorization": f"Bearer {_X_BEARER}",
        "X-CSRF-Token": ct0,
        "Cookie": f"auth_token={auth}; ct0={ct0}",
        "Referer": "https://x.com/",
        "X-Twitter-Active-User": "yes",
        "X-Twitter-Client-Language": "en",
    }


def _x_get(url, headers, params):
    last_err, last_resp = None, None
    for _ in range(3):
        try:
            r = requests.get(url, headers=headers, params=params, timeout=25)
            if r.status_code == 429:  # 限流：等一等再试
                time.sleep(8)
                last_resp = r
                continue
            return r
        except Exception as e:
            last_err = e
            time.sleep(3)
    if last_err:
        raise last_err
    return last_resp


def _x_query_ids():
    """优先从社区维护的接口文档取最新指纹，失败用兜底值"""
    try:
        r = requests.get(
            "https://cdn.jsdelivr.net/gh/fa0311/TwitterInternalAPIDocument@master/docs/json/API.json",
            timeout=15)
        g = r.json().get("graphql", {})
        ids = {k: g[k]["queryId"] for k in _X_FALLBACK_QIDS
               if isinstance(g.get(k), dict) and g[k].get("queryId")}
        if ids:
            return {**_X_FALLBACK_QIDS, **ids}
    except Exception:
        pass
    return dict(_X_FALLBACK_QIDS)


def fetch_x_graphql(handle, cfg):
    auth = secret("TWITTER_AUTH_TOKEN", cfg.get("twitter_auth_token", ""))
    if not auth:
        raise RuntimeError("TWITTER_AUTH_TOKEN 未配置")
    h = _x_headers(auth)
    qids = _x_query_ids()

    uid = _UID_CACHE.get(handle)
    if not uid:
        r = _x_get(
            f"https://x.com/i/api/graphql/{qids['UserByScreenName']}/UserByScreenName",
            h, {"variables": json.dumps({"screen_name": handle, "withGrokTranslatedBio": False}),
                "features": json.dumps(_X_FEATURES_USER)})
        r.raise_for_status()
        uid = r.json()["data"]["user"]["result"]["rest_id"]
        _UID_CACHE[handle] = uid

    r = _x_get(
        f"https://x.com/i/api/graphql/{qids['UserTweets']}/UserTweets",
        h, {"variables": json.dumps({"userId": uid, "count": 20,
                                     "includePromotedContent": False,
                                     "withQuickPromoteEligibilityTweetField": True,
                                     "withVoice": False}),
            "features": json.dumps(_X_FEATURES_FEED)})
    r.raise_for_status()

    out = []
    data = r.json().get("data", {}).get("user", {}).get("result", {})
    instrs = (data.get("timeline_v2") or data.get("timeline") or {}).get("timeline", {}).get("instructions", [])
    for ins in instrs:
        for entry in ins.get("entries") or []:
            if not str(entry.get("entryId", "")).startswith("tweet-"):
                continue
            tw = entry.get("content", {}).get("itemContent", {}).get("tweet_results", {}).get("result", {})
            if tw.get("__typename") == "TweetWithVisibilityResults":
                tw = tw.get("tweet") or {}
            leg = tw.get("legacy") or {}
            tid = leg.get("id_str")
            if not tid:
                continue
            created = None
            try:
                created = datetime.strptime(leg.get("created_at", ""), "%a %b %d %H:%M:%S %z %Y")
            except Exception:
                pass
            name = (tw.get("core", {}).get("user_results", {}).get("result", {})
                    .get("legacy", {}).get("name")) or handle
            out.append({
                "id": tid,
                "handle": handle,
                "author": name,
                "text": leg.get("full_text", ""),
                "created_at": created,
                "url": f"https://x.com/{handle}/status/{tid}",
            })
    return out


PROVIDERS["x_graphql"] = fetch_x_graphql


# ------------------------------------------------------------
# 关键词粗筛
# ------------------------------------------------------------

def keyword_filter(text, cfg):
    """返回命中的关键词列表；None 表示被过滤掉"""
    low = text.lower()
    for bad in cfg.get("exclude_keywords") or []:
        if bad.lower() in low:
            return None
    hits = [k for k in (cfg.get("keywords") or []) if k.lower() in low]
    return hits or None


# ------------------------------------------------------------
# 扩展信息源：财经日历 / SEC EDGAR / RSS快讯 / 币安公告（进事件流）
#             + 行情/资金费率/恐惧贪婪/Polymarket（进快照）
# 全部免费免密钥（配置见 sources.yaml）；单个源失败只记日志，不影响主流程
# ------------------------------------------------------------

_EDGAR_ITEMS = {
    "1.01": "重大协议", "1.02": "协议终止", "1.05": "网络安全事件",
    "2.02": "经营业绩/财报", "2.03": "新财务义务", "2.04": "触发加速/违约",
    "2.05": "退出与处置", "4.02": "审计意见变化", "5.02": "高管董事变动",
    "8.01": "其他重要事件",
}

_LEVEL_EMOJI = {"L4": "🟣", "L3": "🔴", "L2": "🟠", "L1": "⚪"}


def _sub(cfg, name):
    return (cfg.get("sources") or {}).get(name) or {}


def _get_json(url, params=None, headers=None, timeout=25):
    r = requests.get(url, params=params, timeout=timeout,
                     headers={**_HTTP_HEADERS, **(headers or {})})
    r.raise_for_status()
    return r.json()


def _parse_dt(s):
    """容错解析时间字符串 -> aware datetime；解析不了返回 None"""
    if s is None or s == "":
        return None
    s = str(s).strip()
    if s.isdigit() and len(s) >= 12:  # 毫秒时间戳
        try:
            return datetime.fromtimestamp(int(s) / 1000, tz=timezone.utc)
        except Exception:
            return None
    s = s.replace("Z", "+00:00")
    try:
        return datetime.strptime(s, "%a %b %d %H:%M:%S %z %Y")
    except Exception:
        pass
    try:
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _grade_calendar(title, impact):
    """按事件交易手册的 L1-L4 粗定级；L4(格局级)无法自动识别，需人工判断"""
    t = (title or "").lower()
    if "fomc member" in t:
        return "L2" if impact == "High" else "L1"
    if "fomc" in t or "federal funds" in t:
        return "L3"
    if any(k in t for k in ("powell", "fed chair", "jackson hole")):
        return "L2"
    if any(k in t for k in ("cpi", "non-farm", "nonfarm", "pce")):
        return "L2"
    if impact == "High":
        return "L2"
    return "L1"


# FOMC 决议日（每年1月美联储官网公布全年日程后，手动更新一次）
_FOMC_DECISIONS = {2026: [(9, 16), (10, 28), (12, 9)]}


def _nth_sunday(year, month, n):
    d = datetime(year, month, 1, tzinfo=timezone.utc)
    return d + timedelta(days=(6 - d.weekday()) % 7 + (n - 1) * 7)


def _us_et_event(year, month, day, hour_et, minute_et=0):
    """美东时间 -> UTC aware datetime（自动判断夏令时）"""
    d = datetime(year, month, day, tzinfo=timezone.utc)
    dst_start, dst_end = _nth_sunday(year, 3, 2), _nth_sunday(year, 11, 1)
    offset = 4 if dst_start <= d < dst_end else 5
    et = timezone(timedelta(hours=-offset))
    return datetime(year, month, day, hour_et, minute_et, tzinfo=et).astimezone(timezone.utc)


def _mk_cal_event(dt_utc, title, level, forecast="", previous="", approx=False):
    dt_cst = dt_utc.astimezone(CST)
    return {
        "date": dt_cst.strftime("%Y-%m-%d"), "time": dt_cst.strftime("%H:%M"),
        "ts": dt_cst.isoformat(), "currency": "USD",
        "title": title + ("（预估日期）" if approx else ""),
        "impact": "High" if level in ("L2", "L3") else "Medium",
        "level": level, "forecast": forecast, "previous": previous,
        "approx": approx,
    }


def _recurring_macro_events(start_cst, end_cst, ff_events):
    """推算未来30天的周期性宏观数据（FF周历只覆盖本周，其余靠推算+固定表）
    非农(第一个周五)/FOMC(固定日程)日期是确定的；CPI/PCE为惯例日期，标注预估"""
    evs = []
    ff_by_date = {}
    for e in ff_events:
        ff_by_date.setdefault(e["date"], []).append(e["title"].lower())

    def ff_has(key, day):
        return any(key in t for t in ff_by_date.get(day.isoformat(), []))

    def bus_day_on_or_after(y, m, d0):
        d = date(y, m, d0)
        while d.weekday() >= 5:  # 周末顺延
            d += timedelta(days=1)
        return d

    months = set()
    d = start_cst.date()
    while d <= end_cst.date():
        months.add((d.year, d.month))
        d += timedelta(days=1)

    for (y, m) in sorted(months):
        first = date(y, m, 1)
        # 非农：每月第一个周五 8:30 ET（日期确定）
        nfp = first + timedelta(days=(4 - first.weekday()) % 7)
        if start_cst.date() <= nfp <= end_cst.date() \
                and not ff_has("non-farm", nfp) and not ff_has("nonfarm", nfp):
            evs.append(_mk_cal_event(_us_et_event(y, nfp.month, nfp.day, 8, 30),
                                     "美国非农就业报告", "L2"))
        # CPI：每月10日起首个工作日 8:30 ET（BLS实际日期以公告为准）
        cpi = bus_day_on_or_after(y, m, 10)
        if start_cst.date() <= cpi <= end_cst.date() and not ff_has("cpi", cpi):
            evs.append(_mk_cal_event(_us_et_event(y, cpi.month, cpi.day, 8, 30),
                                     "美国CPI通胀数据", "L2", approx=True))
        # PCE：当月最后一个工作日 8:30 ET（BEA实际日期以公告为准）
        last = (date(y, m + 1, 1) if m < 12 else date(y + 1, 1, 1)) - timedelta(days=1)
        while last.weekday() >= 5:
            last -= timedelta(days=1)
        if start_cst.date() <= last <= end_cst.date() and not ff_has("pce", last):
            evs.append(_mk_cal_event(_us_et_event(y, last.month, last.day, 8, 30),
                                     "美国PCE物价指数", "L2", approx=True))

    # FOMC 决议：固定日程表 14:00 ET
    for y, dts in _FOMC_DECISIONS.items():
        for (mm, dd) in dts:
            fd = date(y, mm, dd)
            if start_cst.date() <= fd <= end_cst.date() \
                    and not ff_has("fomc", fd) and not ff_has("federal funds", fd):
                evs.append(_mk_cal_event(_us_et_event(y, mm, dd, 14),
                                         "FOMC利率决议", "L3"))
    return evs


CAL_TRANS_PATH = os.path.join(DATA_DIR, "cal_trans.json")  # 日历标题翻译缓存


# ForexFactory 常见事件标题中文词典(小写精确匹配;未命中的走AI翻译并缓存)
_CAL_ZH = {
    "cpi m/m": "CPI环比", "cpi y/y": "CPI同比",
    "core cpi m/m": "核心CPI环比", "core cpi y/y": "核心CPI同比",
    "non-farm employment change": "非农就业人数", "nonfarm payrolls": "非农就业",
    "unemployment rate": "失业率", "average hourly earnings m/m": "平均时薪环比",
    "fomc statement": "FOMC声明", "federal funds rate": "联邦基金利率",
    "fomc economic projections": "FOMC经济预测", "fomc press conference": "FOMC新闻发布会",
    "core pce price index m/m": "核心PCE环比", "core pce price index y/y": "核心PCE同比",
    "pce price index m/m": "PCE环比", "pce price index y/y": "PCE同比",
    "personal spending m/m": "个人消费支出环比", "personal income m/m": "个人收入环比",
    "retail sales m/m": "零售销售环比", "core retail sales m/m": "核心零售销售环比",
    "advance retail sales m/m": "零售销售环比(初值)",
    "initial jobless claims": "初请失业金人数", "continuing jobless claims": "续请失业金人数",
    "ism manufacturing pmi": "ISM制造业PMI", "ism services pmi": "ISM服务业PMI",
    "manufacturing pmi": "制造业PMI", "services pmi": "服务业PMI",
    "gdp q/q": "GDP环比(年化)", "advance gdp q/q": "GDP初值", "final gdp q/q": "GDP终值",
    "gdp price index q/q": "GDP平减指数",
    "crude oil inventories": "EIA原油库存", "api crude oil stock change": "API原油库存",
    "prelim uom consumer sentiment": "密歇根消费者信心(初值)",
    "final uom consumer sentiment": "密歇根消费者信心(终值)",
    "revised uom consumer sentiment": "密歇根消费者信心(修正)",
    "uom inflation expectations": "密歇根通胀预期",
    "revised uom inflation expectations": "密歇根通胀预期(修正)",
    "building permits": "营建许可", "housing starts": "新屋开工",
    "new home sales": "新屋销售", "existing home sales": "成屋销售",
    "durable goods orders": "耐用品订单", "core durable goods orders": "核心耐用品订单",
    "adp employment change": "ADP就业人数", "jolts job openings": "JOLTS职位空缺",
    "philly fed manufacturing index": "费城联储制造业指数",
    "empire state manufacturing index": "纽约联储制造业指数",
    "richmond manufacturing index": "里士满联储制造业指数",
    "chicago pmi": "芝加哥PMI", "kansas city fed manufacturing index": "堪萨斯联储制造业指数",
    "factory orders m/m": "工厂订单环比", "wholesale inventories m/m": "批发库存环比",
    "business inventories m/m": "商业库存环比", "construction spending m/m": "营建支出环比",
    "ppi m/m": "PPI环比", "core ppi m/m": "核心PPI环比",
    "trade balance": "贸易帐", "beige book": "美联储褐皮书",
    "federal budget balance": "联邦预算", "consumer credit": "消费信贷",
    "mba mortgage applications": "MBA抵押贷款申请",
    "nahb housing market index": "NAHB房产市场指数",
    "unit labour cost q/q": "单位劳动力成本", "nonfarm productivity q/q": "非农生产率",
    "adp non-farm employment change": "ADP非农就业",
    "unemployment claims": "初请失业金人数",
    "natural gas storage": "天然气库存",
    "ism manufacturing prices": "ISM制造业物价指数",
    "final manufacturing pmi": "制造业PMI(终值)",
    "final services pmi": "服务业PMI(终值)",
    "non-manufacturing pmi": "非制造业PMI",
    "challenger job cuts y/y": "挑战者裁员人数同比",
    "prelim benchmark payrolls revision": "非农基准修正(初值)",
    "final benchmark payrolls revision": "非农基准修正(终值)",
}


def _zh_title(title, trans_cache=None):
    """日历标题翻译:词典 → 后缀规则 → 翻译缓存;都没有返回空串"""
    t = (title or "").strip()
    tl = t.lower()
    if not tl:
        return ""
    if tl in _CAL_ZH:
        return _CAL_ZH[tl]
    for suf, zh in ((" m/m", "环比"), (" y/y", "同比"), (" q/q", "环比年化")):
        if tl.endswith(suf):
            base = _CAL_ZH.get(tl[:-len(suf)])
            if base:
                return base + zh
    if tl.startswith("fomc member"):
        return "FOMC官员讲话"
    if "fed chair" in tl or tl.startswith("fed chairman"):
        return "美联储主席讲话"
    if "treasury secretary" in tl:
        return "美国财政部长讲话"
    if "jackson hole" in tl:
        return "Jackson Hole央行年会"
    if trans_cache and tl in trans_cache:
        return trans_cache[tl]
    return ""


def _ai_translate_titles(titles, cfg):
    """用AI接力链批量翻译日历标题;返回 {英文:中文},失败返回空dict"""
    if not titles:
        return {}
    key = os.environ.get("AI_API_KEY", "")
    base = (cfg.get("ai_base_url") or "").rstrip("/")
    if not (key and base):
        return {}
    prompt = ("你是财经编辑。把下面JSON数组里的宏观经济事件名称翻译成简洁的中文金融术语,"
              "保留PMI/CPI等必要缩写。严格只输出一个JSON数组,顺序与输入一致。输入:\n"
              + json.dumps(titles, ensure_ascii=False))
    headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}
    for model in (cfg.get("ai_models") or []):
        for _ in range(2):
            try:
                resp = requests.post(
                    f"{base}/chat/completions", headers=headers,
                    json={"model": model,
                          "messages": [{"role": "user", "content": prompt}],
                          "temperature": 0, "max_tokens": 2000,
                          "reasoning": {"exclude": True}},
                    timeout=60)
                if resp.status_code == 429:
                    time.sleep(8)
                    continue
                resp.raise_for_status()
                text = resp.json()["choices"][0]["message"]["content"]
                text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
                m = re.search(r"\[[^\[\]]*\]", text, re.S)
                if not m:
                    break
                zh = json.loads(m.group(0))
                if isinstance(zh, list) and len(zh) == len(titles):
                    return {t: str(z) for t, z in zip(titles, zh)}
                break
            except Exception:
                time.sleep(3)
    return {}


def fetch_ff_calendar(src_cfg, out_errors):
    """未来30天财经日历 = FF本周精确数据(免key) + 周期性宏观数据推算 + FOMC固定表"""
    currencies = set(src_cfg.get("currencies") or ["USD", "CNY"])
    events, seen = [], set()

    def add(e):
        key = (e["currency"], e["title"], e["date"] + e["time"])
        if key not in seen:
            seen.add(key)
            events.append(e)

    ff_rows = []
    try:
        ff_rows = _get_json("https://nfs.faireconomy.media/ff_calendar_thisweek.json")
    except Exception as e:
        out_errors.append(f"ff_calendar[thisweek]: {e}")
    for row in ff_rows:
        cur = row.get("country") or row.get("currency") or ""
        if cur not in currencies:
            continue
        dt = _parse_dt(row.get("date"))
        if not dt:
            continue
        dt_cst = dt.astimezone(CST)
        add({
            "date": dt_cst.strftime("%Y-%m-%d"), "time": dt_cst.strftime("%H:%M"),
            "ts": dt_cst.isoformat(), "currency": cur,
            "title": row.get("title") or "", "impact": row.get("impact") or "",
            "level": _grade_calendar(row.get("title") or "", row.get("impact") or ""),
            "forecast": row.get("forecast") or "", "previous": row.get("previous") or "",
            "approx": False,
        })

    today0 = datetime.now(CST).replace(hour=0, minute=0, second=0, microsecond=0)
    end = today0 + timedelta(days=30)
    for e in _recurring_macro_events(today0, end, events):
        add(e)

    events.sort(key=lambda e: e["ts"])
    return [e for e in events
            if today0 <= _parse_dt(e["ts"]).astimezone(CST) <= end]


def fetch_binance_ann(src_cfg, out_errors):
    """币安公告：list/query接口(含发布时间)优先，catalog接口与公共RSSHub兜底"""
    urls = [
        ("https://www.binance.com/bapi/composite/v1/public/cms/article/list/query"
         "?type=1&pageNo=1&pageSize=20&catalogId=48", "list"),
        ("https://www.binance.com/bapi/composite/v1/public/cms/article/catalog/list/query"
         "?catalogId=48&pageNo=1&pageSize=20", "catalog"),
        ("https://rsshub.ktachibana.party/binance/announcements", "rss"),
    ]
    for url, kind in urls:
        try:
            r = requests.get(url, headers=_HTTP_HEADERS, timeout=25)
            r.raise_for_status()
            out = []
            if kind == "rss":
                root = ET.fromstring(r.content)
                for it in root.iter("item"):
                    link = (it.findtext("link") or "").strip()
                    title = (it.findtext("title") or "").strip()
                    if not (link and title):
                        continue
                    out.append({
                        "id": "binance:" + hashlib.md5(link.encode()).hexdigest()[:16],
                        "source": "binance", "author": "币安公告", "text": title,
                        "created_at": _parse_dt(it.findtext("pubDate")), "url": link,
                    })
            else:
                data = r.json().get("data") or {}
                arts = data if isinstance(data, list) else []
                if not arts:
                    for cat in data.get("catalogs") or []:
                        arts.extend(cat.get("articles") or [])
                for a in arts:
                    aid, title = str(a.get("id") or ""), a.get("title") or ""
                    if not (aid and title):
                        continue
                    out.append({
                        "id": f"binance:{aid}", "source": "binance",
                        "author": "币安公告", "text": title,
                        "created_at": _parse_dt(a.get("releaseDate")),
                        "url": f"https://www.binance.com/zh-CN/square/post/{aid}",
                    })
            if out:
                return out
        except Exception as e:
            out_errors.append(f"binance_ann[{kind}]: {e}")
    return []


def fetch_edgar(src_cfg, out_errors):
    """SEC EDGAR 白名单公司近3天新文件（8-K/财报等）"""
    out = []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=3))
    for name, cik in (src_cfg.get("companies") or {}).items():
        try:
            j = _get_json(
                f"https://data.sec.gov/submissions/CIK{cik}.json",
                headers={"User-Agent": "finance-monitor research example@example.com"})
            recent = j.get("filings", {}).get("recent", {})
            forms = recent.get("form", [])
            for i, form in enumerate(forms):
                try:
                    fdate = datetime.fromisoformat(recent["filingDate"][i]).replace(
                        tzinfo=timezone.utc)
                except Exception:
                    continue
                if fdate < cutoff:
                    continue
                accn = recent["accessionNumber"][i].replace("-", "")
                its = recent.get("items") or []
                raw = its[i] if i < len(its) else ""
                desc = "、".join(_EDGAR_ITEMS.get(x.strip(), x.strip())
                                for x in raw.split(",") if x.strip())
                text = f"{name} 提交SEC文件 [{form}]" + (f"（{desc}）" if desc else "")
                out.append({
                    "id": f"edgar:{accn}", "source": "edgar", "author": name,
                    "text": text, "created_at": fdate,
                    "url": f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accn}/",
                })
        except Exception as e:
            out_errors.append(f"edgar[{name}]: {e}")
    return out


def fetch_rss_feeds(src_cfg, out_errors):
    """通用RSS快讯（每源取最新15条；feeds 支持 url 单地址或 urls 兜底链）"""
    out = []
    for feed in src_cfg.get("feeds") or []:
        name = feed.get("name") or "rss"
        urls = feed.get("urls") or ([feed.get("url")] if feed.get("url") else [])
        got = None
        for url in urls:
            try:
                r = requests.get(url, headers=_HTTP_HEADERS, timeout=25)
                r.raise_for_status()
                got = r.content
                break
            except Exception as e:
                out_errors.append(f"rss[{name}]: {e}")
        if got is None:
            continue
        try:
            root = ET.fromstring(got)
            n = 0
            for it in root.iter("item"):
                link = (it.findtext("link") or "").strip()
                title = html.unescape((it.findtext("title") or "").strip())
                if not (link and title):
                    continue
                desc = html.unescape(re.sub(r"<[^>]+>", " ",
                                            it.findtext("description") or ""))
                desc = re.sub(r"\s+", " ", desc).strip()[:300]
                out.append({
                    "id": "rss:" + hashlib.md5(link.encode()).hexdigest()[:16],
                    "source": "rss", "author": name,
                    "text": title + (f" —— {desc}" if desc else ""),
                    "created_at": _parse_dt(it.findtext("pubDate")), "url": link,
                })
                n += 1
                if n >= 15:
                    break
        except Exception as e:
            out_errors.append(f"rss[{name}]解析: {e}")
    return out


def collect_market(cfg, out_errors):
    """行情+情绪读数快照（只写 market.json 给网站/晨会用，不推送）"""
    snap = {"generated_at": datetime.now(CST).isoformat(),
            "quotes": [], "fng": None, "funding": [], "polymarket": []}
    m = _sub(cfg, "market")
    if m.get("enabled"):
        for name, sym in (m.get("symbols") or {}).items():
            try:
                j = _get_json(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}",
                              params={"interval": "1d", "range": "5d"})
                meta = j.get("chart", {}).get("result", [{}])[0].get("meta", {})
                price, prev = meta.get("regularMarketPrice"), (
                    meta.get("chartPreviousClose") or meta.get("previousClose"))
                snap["quotes"].append({
                    "name": name, "symbol": sym, "price": price, "previous": prev,
                    "pct": round((price - prev) / prev * 100, 2)
                    if (price and prev) else None})
            except Exception as e:
                out_errors.append(f"market[{name}]: {e}")
    if _sub(cfg, "fng").get("enabled"):
        try:
            d = _get_json("https://api.alternative.me/fng/", params={"limit": 1})["data"][0]
            snap["fng"] = {"value": int(d["value"]),
                           "label": d.get("value_classification") or ""}
        except Exception as e:
            out_errors.append(f"fng: {e}")
    fu = _sub(cfg, "funding")
    if fu.get("enabled"):
        for sym in fu.get("symbols") or ["BTCUSDT", "ETHUSDT"]:
            rate = None
            try:
                lst = _get_json("https://api.bybit.com/v5/market/tickers",
                                params={"category": "linear", "symbol": sym}
                                ).get("result", {}).get("list", [])
                if lst:
                    rate = float(lst[0].get("fundingRate") or 0)
            except Exception:
                try:
                    j = _get_json("https://www.okx.com/api/v5/public/funding-rate",
                                  params={"instId": sym.replace("USDT", "-USDT-SWAP")})
                    rate = float(j["data"][0]["fundingRate"])
                except Exception as e:
                    out_errors.append(f"funding[{sym}]: {e}")
            if rate is not None:
                snap["funding"].append({"symbol": sym, "rate": rate,
                                        "rate_pct": round(rate * 100, 4)})
    if _sub(cfg, "polymarket").get("enabled"):
        try:
            rows = _get_json("https://gamma-api.polymarket.com/markets",
                             params={"closed": "false", "limit": 60,
                                     "order": "volume24hr", "ascending": "false"})
            picked = 0
            for mk in rows if isinstance(rows, list) else []:
                q = mk.get("question") or ""
                if picked >= 4:
                    break
                low = q.lower()
                if not any(k in low for k in ("fed", "rate cut", "rate hike",
                                              "cpi", "interest rate")):
                    continue
                try:
                    outcomes = json.loads(mk.get("outcomes") or "[]")
                    prices = json.loads(mk.get("outcomePrices") or "[]")
                    yes = float(prices[outcomes.index("Yes")])
                except Exception:
                    continue
                snap["polymarket"].append({"question": q, "yes": round(yes * 100, 1)})
                picked += 1
        except Exception as e:
            out_errors.append(f"polymarket: {e}")
    return snap


def run_extra_sources(cfg, state, stats, store):
    """扩展源入库（进网站事件流）；score>=push_min_score 才推企微"""
    scfg = load_sources_cfg()
    srcs = scfg.get("sources") or {}
    errors = []
    seen_src = state.setdefault("seen_src", {})
    known = {e.get("id") for e in store}
    push_min = int(scfg.get("push_min_score", 8) or 0)
    ai_budget = int(scfg.get("max_ai_per_run", 8) or 0)

    # 日历（整体重建，无需去重）
    ff = srcs.get("ff_calendar") or {}
    if ff.get("enabled"):
        try:
            cal = fetch_ff_calendar(ff, errors)
            # 中文标题:词典/缓存优先,未命中的批量走AI翻译并缓存
            cache = {}
            if os.path.exists(CAL_TRANS_PATH):
                try:
                    with open(CAL_TRANS_PATH, "r", encoding="utf-8") as f:
                        cache = json.load(f)
                except Exception:
                    cache = {}
            def _has_cjk(s):
                return any('一' <= ch <= '鿿' for ch in s)
            unknown = sorted({e["title"] for e in cal
                              if e["title"] and not _has_cjk(e["title"])
                              and not _zh_title(e["title"])
                              and e["title"] not in cache})
            if unknown and cfg.get("ai_enabled"):
                got = _ai_translate_titles(unknown, cfg)
                if got:
                    cache.update(got)
                    try:
                        with open(CAL_TRANS_PATH, "w", encoding="utf-8") as f:
                            json.dump(cache, f, ensure_ascii=False, indent=1)
                    except Exception:
                        pass
            for e in cal:
                zh = _zh_title(e["title"], cache)
                if zh:
                    e["title_zh"] = zh
            _write_json(CALENDAR_PATH, {"generated_at": datetime.now(CST).isoformat(),
                                        "events": cal})
            stats["calendar"] = len(cal)
        except Exception as e:
            errors.append(f"ff_calendar: {e}")

    # 事件类来源 -> 关键词粗筛 -> AI评级 -> 入库/推送
    items = []
    for fetcher, sc in ((fetch_binance_ann, srcs.get("binance_ann") or {}),
                        (fetch_edgar, srcs.get("edgar") or {}),
                        (fetch_rss_feeds, srcs.get("rss") or {})):
        if sc.get("enabled"):
            try:
                items.extend(fetcher(sc, errors))
            except Exception as e:
                errors.append(f"{fetcher.__name__}: {e}")

    for it in items:
        if it["id"] in seen_src or it["id"] in known:
            continue
        if not keyword_filter(it["text"], cfg):
            continue
        ai = None
        if cfg.get("ai_enabled") and ai_budget > 0:
            ai = ai_evaluate(it["text"], cfg)
            ai_budget -= 1
        score = ai["score"] if ai and ai.get("ok") else None
        created = (it["created_at"] or datetime.now(timezone.utc)).astimezone(CST)
        rec = {"id": it["id"], "source": it["source"], "author": it["author"],
               "text": it["text"], "score": score,
               "reason": (ai or {}).get("reason", ""),
               "translation": (ai or {}).get("translation", ""),
               "url": it["url"], "pushed": False,
               "created_at": created.isoformat()}
        store.append(rec)
        seen_src[it["id"]] = rec["created_at"]
        stats["extra_stored"] += 1
        if score and push_min and score >= push_min:
            tweet = {"handle": it["author"], "text": it["text"],
                     "created_at": created, "url": it["url"]}
            try:
                title, content = build_message_wecom(tweet, [it["source"]], ai,
                                                     it["source"])
                push(title, content, cfg.get("push_channel", "wecom"), cfg)
                rec["pushed"] = True
                stats["extra_pushed"] += 1
                log(f"📤 扩展源推送 [{it['source']}] {it['text'][:40]}...")
            except Exception as e:
                log(f"❌ 扩展源推送失败: {e}")

    # 状态修剪（只留100天）
    cutoff = (datetime.now(CST) - timedelta(days=100)).isoformat()
    for k in [k for k, v in seen_src.items() if v < cutoff]:
        del seen_src[k]
    if errors:
        log("⚠️ 扩展源部分失败: " + " | ".join(errors[:8]))
    return errors


def _fmt_price(p):
    if p is None:
        return "-"
    return f"{p:,.0f}" if abs(p) >= 1000 else f"{p:g}"


def build_brief(cfg):
    """每日晨会：今日事件 + 未来7天看点 + 隔夜行情 + 情绪读数"""
    today = datetime.now(CST)
    cal, mkt = [], {}
    if os.path.exists(CALENDAR_PATH):
        with open(CALENDAR_PATH, "r", encoding="utf-8") as f:
            cal = json.load(f).get("events", [])
    if os.path.exists(MARKET_PATH):
        with open(MARKET_PATH, "r", encoding="utf-8") as f:
            mkt = json.load(f)

    lines = [f"### 🌅 每日晨会 {today:%m-%d} {['一','二','三','四','五','六','日'][today.weekday()]}"]
    d0 = today.strftime("%Y-%m-%d")
    d7 = (today + timedelta(days=7)).strftime("%Y-%m-%d")
    today_ev = [e for e in cal if e["date"] == d0]
    week_ev = [e for e in cal if d0 < e["date"] <= d7 and e["level"] in ("L2", "L3", "L4")]

    def zh(e):
        return e.get("title_zh") or e.get("title") or ""

    lines.append("**📅 今日事件（北京时间）**")
    if today_ev:
        for e in today_ev[:10]:
            fc = f" 预期{e['forecast']}" if e.get("forecast") else ""
            pv = f" 前值{e['previous']}" if e.get("previous") else ""
            lines.append(f"- {_LEVEL_EMOJI.get(e['level'],'⚪')} **{e['level']}** "
                         f"{e['time']} {e.get('title_zh') or e['title']}({e['currency']}){fc}{pv}")
    else:
        lines.append("- 今日无重要日历事件")

    if week_ev:
        lines.append(f"**🔭 未来7天看点（L2+，共{len(week_ev)}件）**")
        for e in week_ev[:8]:
            lines.append(f"- {_LEVEL_EMOJI.get(e['level'],'⚪')} {e['level']} "
                         f"{e['date'][5:]} {e['time']} {e.get('title_zh') or e['title']}({e['currency']})")

    quotes = mkt.get("quotes") or []
    if quotes:
        qm = {q["name"]: q for q in quotes}
        order = ["比特币", "以太坊", "纳指期货", "美债30Y", "WTI原油", "黄金"]
        parts = []
        for name in order:
            q = qm.get(name)
            if q and q.get("price") is not None:
                pct = f"({q['pct']:+.1f}%)" if q.get("pct") is not None else ""
                parts.append(f"{name} {_fmt_price(q['price'])}{pct}")
        if parts:
            lines.append("**📉 隔夜市场**\n" + " ｜ ".join(parts))

    emo = []
    if mkt.get("fng"):
        emo.append(f"恐惧贪婪 {mkt['fng']['value']}·{mkt['fng']['label']}")
    for f in (mkt.get("funding") or [])[:2]:
        emo.append(f"费率{f['symbol'][:3]} {f['rate_pct']:+.4f}%")
    for p in (mkt.get("polymarket") or [])[:2]:
        emo.append(f"{p['question'][:30]}… Yes {p['yes']:.0f}%")
    if emo:
        lines.append("**🌡️ 情绪读数**\n" + " ｜ ".join(emo))

    hot = [e for e in today_ev if e["level"] in ("L2", "L3", "L4")]
    if hot:
        lines.append(f"**⚠️ 仓位提醒**：今日有 {len(hot)} 件 L2+ 事件"
                     "——不赌方向的仓位在事件公布前1小时清理")
    title = f"🌅 每日晨会 {today:%m-%d}"
    return title, _clip("\n\n".join(lines))


def selftest_sources():
    """扩展源连通性自检（--selftest-sources）；半数以上通过返回0"""
    scfg = load_sources_cfg()
    srcs = scfg.get("sources") or {}
    errors = []
    results = []

    def check(name, fn):
        if not (srcs.get(name) or {}).get("enabled"):
            return
        try:
            n = fn()
            results.append((name, n))
            print(f"✅ {name}: {n} 条")
        except Exception as e:
            results.append((name, -1))
            print(f"❌ {name}: {e}")

    check("ff_calendar", lambda: len(fetch_ff_calendar(srcs["ff_calendar"], errors)))
    check("binance_ann", lambda: len(fetch_binance_ann(srcs["binance_ann"], errors)))
    check("edgar", lambda: len(fetch_edgar(srcs["edgar"], errors)))
    check("rss", lambda: len(fetch_rss_feeds(srcs["rss"], errors)))

    if any(_sub(scfg, k).get("enabled")
           for k in ("market", "fng", "funding", "polymarket")):
        try:
            snap = collect_market(scfg, errors)
            nq = len(snap["quotes"])
            results.append(("market快照", nq))
            print(f"✅ market快照: 行情{nq} 费率{len(snap['funding'])} "
                  f"F&G={'✓' if snap['fng'] else '✗'} "
                  f"Polymarket={len(snap['polymarket'])}")
        except Exception as e:
            results.append(("market快照", -1))
            print(f"❌ market快照: {e}")

    if errors:
        print("失败明细: " + " | ".join(errors))
    ok = sum(1 for _, n in results if n and n > 0)
    print(f"\n通过 {ok}/{len(results)}")
    return 0 if len(results) and ok * 2 >= len(results) else 1


# ------------------------------------------------------------
# AI 精筛（Gemini 免费额度）
# ------------------------------------------------------------

PROMPT_TMPL = """你是资深金融市场分析师。分析下面这条社交媒体发言对金融市场（美股/A股/加密货币）的潜在影响。

发言内容：
{text}

严格只输出JSON（不要多余文字）：
{{"score": 整数1-10, "reason": "一句话中文理由", "translation": "整条发言的中文翻译", "event_time": "事件发生时间"}}
评分标准：9-10=重大(央行决议/开战/重大监管)；7-8=显著(政策信号/大额交易/重要人物表态)；4-6=一般相关；1-3=与市场无关
event_time规则：仅当原文明确提到事件发生/将发生的时间点才填写，格式尽量为"MM-DD HH:MM(时区)"（保持原文时区，不要换算）；原文没提时间点就填空字符串""。注意区分"发言发布时间"与"事件时间"，只要后者。"""


def _ai_parse(raw):
    """容错解析：剥掉推理型模型的思考块和代码围栏；失败则提取首个花括号JSON再试"""
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.S).strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw).strip()
    body = raw
    if not body.startswith("{"):
        m = re.search(r"\{.*\}", raw, flags=re.S)
        if m:
            body = m.group(0)
    data = json.loads(body)
    return {
        "score": int(data.get("score", 0)),
        "reason": str(data.get("reason", ""))[:120],
        "translation": str(data.get("translation", ""))[:1000],
        "event_time": str(data.get("event_time", "") or "")[:50],
        "ok": True,
    }


def ai_gemini(text, cfg):
    """Google Gemini 免费额度"""
    key = os.environ.get("GEMINI_API_KEY", "")
    if not key:
        return None
    model = cfg.get("gemini_model", "gemini-2.5-flash")
    resp = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        params={"key": key},
        json={
            "contents": [{"parts": [{"text": PROMPT_TMPL.format(text=text[:2000])}]}],
            "generationConfig": {"temperature": 0.1, "maxOutputTokens": 800},
        },
        timeout=30,
    )
    resp.raise_for_status()
    return _ai_parse(resp.json()["candidates"][0]["content"]["parts"][0]["text"])


def ai_openai_compat(text, cfg):
    """OpenAI 兼容接口（OpenRouter 等）；免费模型按列表接力，限流/坏输出自动切换"""
    key = os.environ.get("AI_API_KEY", "")
    base = (cfg.get("ai_base_url") or "").rstrip("/")
    if not (key and base):
        return None
    models = cfg.get("ai_models") or [cfg.get("ai_model", "glm-4-flash")]
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    prompt = PROMPT_TMPL.format(text=text[:2000])
    last_err = None
    for model in models:
        for attempt in range(2):  # 每个模型试2次，仍不行就换下一个
            try:
                resp = requests.post(
                    f"{base}/chat/completions",
                    headers=headers,
                    json={
                        "model": model,
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": 0.1,
                        "max_tokens": 2000,
                        "reasoning": {"exclude": True},  # 推理型：不输出思考过程，省配额
                    },
                    timeout=60,
                )
                if resp.status_code == 429:  # 免费池限流：退避后重试一次，再不行换模型
                    time.sleep(10 * (attempt + 1))
                    last_err = RuntimeError(f"{model} 限流(429)")
                    continue
                resp.raise_for_status()
                return _ai_parse(resp.json()["choices"][0]["message"]["content"])
            except Exception as e:
                last_err = e
                time.sleep(3)
        log(f"↪️ {model} 两次尝试失败，切换下一模型…")
    raise last_err


def ai_evaluate(text, cfg):
    """返回 {score, reason, translation, ok}；失败不影响主流程（未评级照推）"""
    if not cfg.get("ai_enabled"):
        return None
    try:
        if cfg.get("ai_provider", "gemini") == "openai_compat":
            return ai_openai_compat(text, cfg)
        return ai_gemini(text, cfg)
    except Exception as e:
        log(f"⚠️ AI评估失败(将按未评级推送): {e}")
        return None


# ------------------------------------------------------------
# 推送（企业微信群机器人 / PushPlus 二选一，config.yaml 里切换）
# ------------------------------------------------------------

SCORE_EMOJI = {10: "🔴🔴", 9: "🔴🔴", 8: "🔴", 7: "🟠", 6: "🟡"}

# 事件时间兜底提取：AI没返回时从原文抓 "周X/今天 H:MM (am/pm) (时区)"
_EVENT_TIME_RE = re.compile(
    r"((?:\b(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*|today|tomorrow|tonight)\s*,?\s*)?"
    r"(?:\bat\s*)?\b(\d{1,2}:\d{2})\s*(am|pm)?\s*"
    r"(ET|EST|EDT|PT|PST|PDT|GMT|UTC|CST|BST|JST|HKT)?\b", re.I)


def _extract_event_time(text, ai):
    """优先用AI提取的事件时间；为空则正则兜底，都没有返回空串"""
    if ai and ai.get("ok") and ai.get("event_time"):
        return ai["event_time"]
    m = _EVENT_TIME_RE.search(text)
    if not m:
        return ""
    day, hm, ampm, tz = (m.group(1) or ""), m.group(2), (m.group(3) or ""), (m.group(4) or "")
    parts = [p.strip() for p in (day, hm, ampm.upper(), tz.upper()) if p.strip()]
    return " ".join(parts)


def _clip(s, limit=3800):
    """企业微信 markdown 上限约4096字节，超长安全截断"""
    return s.encode("utf-8")[:limit].decode("utf-8", "ignore")


def build_message(tweet, kw_hits, ai, note):
    score = ai["score"] if ai and ai.get("ok") else None
    emoji = SCORE_EMOJI.get(score, "⚪") if score else "⚠️"
    level = f"影响力{score}分" if score else "未评级"
    title = f"{emoji}{level} @{tweet['handle']}" + (f"({note})" if note else "")

    t = tweet["created_at"]
    time_str = t.astimezone(CST).strftime("%m-%d %H:%M") if t else ""
    parts = [
        f"<b>👤 @{html.escape(tweet['handle'])}</b>"
        + (f" <small>({html.escape(note)})</small>" if note else ""),
        f"<blockquote>{html.escape(tweet['text'])}</blockquote>",
    ]
    if ai and ai.get("ok") and ai.get("translation"):
        parts.append(f"<b>🇨🇳 中文翻译</b>：<br>{html.escape(ai['translation'])}")
    if ai and ai.get("ok") and ai.get("reason"):
        parts.append(f"<b>📌 判断</b>：{emoji} {level} —— {html.escape(ai['reason'])}")
    elif not (ai and ai.get("ok")):
        parts.append("<i>⚠️ AI 未评级（额度或网络问题），仅关键词命中</i>")
    parts.append(
        f"<b>🔑 命中</b>：{html.escape('、'.join(kw_hits))}"
        + (f"<br><b>⚡ 事件时间</b>：{html.escape(_extract_event_time(tweet['text'], ai))}"
           if _extract_event_time(tweet["text"], ai) else "")
        + (f"<br><b>🕐 发推时间</b>：{time_str}" if time_str else "")
    )
    content = "<br><br>".join(parts) + f'<br><a href="{tweet["url"]}">查看原推 ➡️</a>'
    return title, content


def build_message_wecom(tweet, kw_hits, ai, note):
    """企业微信群机器人 markdown 格式"""
    score = ai["score"] if ai and ai.get("ok") else None
    emoji = SCORE_EMOJI.get(score, "⚪") if score else "⚠️"
    level = f"影响力{score}分" if score else "未评级"

    t = tweet["created_at"]
    time_str = t.astimezone(CST).strftime("%m-%d %H:%M") if t else ""
    lines = [
        f'### {emoji}{level} <font color="comment">@{tweet["handle"]}</font>'
        + (f"（{note}）" if note else ""),
        f"> {tweet['text']}",
    ]
    if ai and ai.get("ok") and ai.get("translation"):
        lines.append(f"**🇨🇳 中文翻译**\n{ai['translation']}")
    if ai and ai.get("ok") and ai.get("reason"):
        lines.append(f"**📌 判断**：{level} —— {ai['reason']}")
    else:
        lines.append("<font color=\"warning\">⚠️ AI未评级（额度/网络问题），仅关键词命中</font>")
    lines.append(f"**🔑 命中**：{'、'.join(kw_hits)}")
    evt = _extract_event_time(tweet["text"], ai)
    if evt:
        lines.append(f"**⚡ 事件时间**：{evt}")
    if time_str:
        lines.append(f"**🕐 发推时间**：{time_str}")
    lines.append(f"[🔗 查看原推]({tweet['url']})")
    title = f"{emoji}{level}@{tweet['handle']}"
    return title, _clip("\n\n".join(lines))


def push(title, content, channel, cfg):
    if os.environ.get("DRY_RUN"):
        log(f"🧪 [模拟推送-{channel}] {title}")
        return
    if channel == "wecom":
        webhook = os.environ.get("WECOM_WEBHOOK") or cfg.get("wecom_webhook", "")
        if not webhook:
            raise RuntimeError("WECOM_WEBHOOK 未配置")
        resp = requests.post(
            webhook,
            json={"msgtype": "markdown", "markdown": {"content": f"## {title}\n{content}"}},
            timeout=20,
        )
        data = resp.json()
        if data.get("errcode") != 0:
            raise RuntimeError(f"企业微信失败: {data}")
        return

    # pushplus 备用通道
    token = os.environ.get("PUSHPLUS_TOKEN", "")
    if not token:
        raise RuntimeError("PUSHPLUS_TOKEN 未配置")
    resp = requests.post(
        "https://www.pushplus.plus/send",
        json={"token": token, "title": title[:100], "content": content, "template": "html"},
        timeout=20,
    )
    data = resp.json()
    if data.get("code") != 200:
        raise RuntimeError(f"PushPlus失败: {data}")
    return True


# ------------------------------------------------------------
# 主流程
# ------------------------------------------------------------

def process_account(account, seen_ids, cfg, stats, store):
    handle = account["handle"].strip().lstrip("@")
    note = account.get("note", "")
    provider_name = cfg.get("provider", "mock")
    fetcher = PROVIDERS.get(provider_name)
    if not fetcher:
        raise RuntimeError(f"未知 provider: {provider_name}")

    tweets = fetcher(handle, cfg)
    tweets.sort(key=lambda t: int(t["id"]))  # 旧->新，保证推送顺序自然

    is_first_run = handle not in seen_ids
    pushed = 0
    for tw in tweets:
        tw_id = tw["id"]
        if int(tw_id) <= int(seen_ids.get(handle, 0)):
            continue
        seen_ids[handle] = tw_id  # 无论是否推送都标记已见，避免重复处理
        if is_first_run:
            continue  # 首次见到该博主：只建基线，不推送历史消息
        hits = keyword_filter(tw["text"], cfg)
        if not hits:
            stats["filtered"] += 1
            continue
        stats["kw_passed"] += 1
        ai = ai_evaluate(tw["text"], cfg)
        if ai and ai.get("ok") and ai["score"] < cfg.get("min_score", 6):
            stats["ai_filtered"] += 1
            continue
        if pushed >= cfg.get("max_push_per_run", 30):
            stats["capped"] += 1
            continue
        channel = cfg.get("push_channel", "wecom")
        builder = build_message_wecom if channel == "wecom" else build_message
        title, content = builder(tw, hits, ai, note)
        try:
            push(title, content, channel, cfg)
            pushed += 1
            stats["pushed"] += 1
            log(f"📤 已推送 @{handle}: {tw['text'][:40]}...")
            # 同时写入网站事件流（data/events.json）
            created = (tw["created_at"] or datetime.now(timezone.utc)).astimezone(CST)
            store.append({
                "id": f"x:{tw_id}", "source": "x", "author": f"@{handle}",
                "text": tw["text"],
                "score": ai["score"] if ai and ai.get("ok") else None,
                "reason": (ai or {}).get("reason", ""),
                "translation": (ai or {}).get("translation", ""),
                "url": tw["url"], "pushed": True,
                "created_at": created.isoformat(),
            })
        except Exception as e:
            log(f"❌ 推送失败 @{handle}: {e}")


def main():
    cfg = load_config()
    accounts = cfg.get("accounts") or []
    log(f"开始监控 {len(accounts)} 个账号 | provider={cfg.get('provider')}")

    state = load_state()
    seen_ids = state.setdefault("seen", {})
    store = load_events()
    stats = {"pushed": 0, "filtered": 0, "kw_passed": 0, "ai_filtered": 0,
             "capped": 0, "extra_stored": 0, "extra_pushed": 0, "calendar": 0}
    errors = []

    for account in accounts:
        try:
            process_account(account, seen_ids, cfg, stats, store)
        except Exception as e:
            errors.append(f"@{account.get('handle')}: {e}")
            log(f"❌ @{account.get('handle')} 处理出错: {e}")

    # 扩展信息源（日历/EDGAR/RSS/币安公告）+ 行情情绪快照；失败不影响X主流程
    extra_errors = []
    try:
        extra_errors = run_extra_sources(cfg, state, stats, store)
    except Exception as e:
        log(f"⚠️ 扩展源异常(不影响X监控): {e}")
    try:
        mk_errors = []
        snap = collect_market(load_sources_cfg(), mk_errors)
        _write_json(MARKET_PATH, snap)
        if mk_errors:
            log("⚠️ 快照部分失败: " + " | ".join(mk_errors[:6]))
    except Exception as e:
        log(f"⚠️ 行情快照异常(不影响X监控): {e}")

    save_events(store)
    save_state(state)
    log(
        f"完成 ✅ 推送{stats['pushed']} | 关键词命中{stats['kw_passed']} "
        f"(AI过滤{stats['ai_filtered']}, 关键词过滤{stats['filtered']}, 截断{stats['capped']}) "
        f"| 扩展源入库{stats['extra_stored']}/推送{stats['extra_pushed']} "
        f"| 日历{stats['calendar']}条"
    )
    if errors:
        print("本次出错账号:\n" + "\n".join(errors))
        sys.exit(1)  # 让 Actions 显示失败便于发现问题，但不影响其他账号已完成的推送


def run_brief(cfg):
    """每日晨会：读取 data/ 下的日历与快照合成一条推送"""
    try:
        title, content = build_brief(cfg)
    except Exception as e:
        title, content = "🌅 晨会生成失败", f"错误: {e}"
    push(title, content, cfg.get("push_channel", "wecom"), cfg)
    log(f"📤 已推送: {title}")


if __name__ == "__main__":
    if "--brief" in sys.argv:
        run_brief(load_config())
    elif "--selftest-sources" in sys.argv:
        sys.exit(selftest_sources())
    else:
        main()
