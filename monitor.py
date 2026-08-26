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
import json
import os
import re
import secrets
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime

import requests
import yaml

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.yaml")
STATE_PATH = os.path.join(BASE_DIR, "state.json")

CST = timezone(timedelta(hours=8))  # 北京时间

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
# AI 精筛（Gemini 免费额度）
# ------------------------------------------------------------

PROMPT_TMPL = """你是资深金融市场分析师。分析下面这条社交媒体发言对金融市场（美股/A股/加密货币）的潜在影响。

发言内容：
{text}

严格只输出JSON（不要多余文字）：
{{"score": 整数1-10, "reason": "一句话中文理由", "translation": "整条发言的中文翻译"}}
评分标准：9-10=重大(央行决议/开战/重大监管)；7-8=显著(政策信号/大额交易/重要人物表态)；4-6=一般相关；1-3=与市场无关"""


def _ai_parse(raw):
    raw = re.sub(r"^```(json)?|```$", "", raw.strip(), flags=re.M).strip()
    data = json.loads(raw)
    return {
        "score": int(data.get("score", 0)),
        "reason": str(data.get("reason", ""))[:120],
        "translation": str(data.get("translation", ""))[:1000],
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
    """任意 OpenAI 兼容接口：智谱/硅基流动/DeepSeek/Kimi/OpenRouter 等"""
    key = os.environ.get("AI_API_KEY", "")
    base = (cfg.get("ai_base_url") or "").rstrip("/")
    if not (key and base):
        return None
    model = cfg.get("ai_model", "glm-4-flash")
    resp = requests.post(
        f"{base}/chat/completions",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={
            "model": model,
            "messages": [{"role": "user", "content": PROMPT_TMPL.format(text=text[:2000])}],
            "temperature": 0.1,
            "max_tokens": 2000,
            "reasoning": {"exclude": True},  # 推理型模型：不输出思考过程，省配额
        },
        timeout=60,
    )
    resp.raise_for_status()
    return _ai_parse(resp.json()["choices"][0]["message"]["content"])


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

SCORE_EMOJI = {9: "🔴🔴", 8: "🔴", 7: "🟠", 6: "🟡"}


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
        + (f"<br><b>🕐 </b>{time_str}" if time_str else "")
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
    if time_str:
        lines.append(f"**🕐 时间**：{time_str}")
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

def process_account(account, seen_ids, cfg, stats):
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
        except Exception as e:
            log(f"❌ 推送失败 @{handle}: {e}")


def main():
    cfg = load_config()
    accounts = cfg.get("accounts") or []
    log(f"开始监控 {len(accounts)} 个账号 | provider={cfg.get('provider')}")

    state = load_state()
    seen_ids = state.setdefault("seen", {})
    stats = {"pushed": 0, "filtered": 0, "kw_passed": 0, "ai_filtered": 0, "capped": 0}
    errors = []

    for account in accounts:
        try:
            process_account(account, seen_ids, cfg, stats)
        except Exception as e:
            errors.append(f"@{account.get('handle')}: {e}")
            log(f"❌ @{account.get('handle')} 处理出错: {e}")

    save_state(state)
    log(
        f"完成 ✅ 推送{stats['pushed']} | 关键词命中{stats['kw_passed']} "
        f"(AI过滤{stats['ai_filtered']}, 关键词过滤{stats['filtered']}, 截断{stats['capped']})"
    )
    if errors:
        print("本次出错账号:\n" + "\n".join(errors))
        sys.exit(1)  # 让 Actions 显示失败便于发现问题，但不影响其他账号已完成的推送


if __name__ == "__main__":
    main()
