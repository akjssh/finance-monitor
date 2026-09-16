# -*- coding: utf-8 -*-
"""
一次性任务：用 SearchTimeline 抓取 @thankUcrypto 2025 全年推文。
查询区间可用环境变量覆盖：X_SINCE / X_UNTIL（默认 2025-01-01 / 2026-01-01）。
复用 monitor.py 的会话头（auth_token 走 Secrets：TWITTER_AUTH_TOKEN）。
输出 data/aoying_history_tweets.json（按时间升序），由 workflow 提交回仓库。
"""
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

import monitor
import requests

HANDLE = "thankUcrypto"
SINCE = os.environ.get("X_SINCE", "2025-01-01")
UNTIL = os.environ.get("X_UNTIL", "2026-01-01")
MAX_PAGES = 500
PAGE_SLEEP = 1.0
OUT_PATH = os.path.join("data", "aoying_history_tweets.json")
GQL_URL = "https://x.com/i/api/graphql"
OP_FALLBACK = "hyPfJYJ_XAtDYoslQc-Rgg"  # twscrape 维护的 SearchTimeline 指纹兜底

# features 集取自 twscrape GQL_FEATURES（2026-09 版本）
FEATURES = {
    "articles_preview_enabled": False,
    "c9s_tweet_anatomy_moderator_badge_enabled": True,
    "communities_web_enable_tweet_community_results_fetch": True,
    "creator_subscriptions_quote_tweet_preview_enabled": False,
    "creator_subscriptions_tweet_preview_api_enabled": True,
    "freedom_of_speech_not_reach_fetch_enabled": True,
    "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
    "longform_notetweets_consumption_enabled": True,
    "longform_notetweets_inline_media_enabled": True,
    "longform_notetweets_rich_text_read_enabled": True,
    "responsive_web_edit_tweet_api_enabled": True,
    "responsive_web_enhance_cards_enabled": False,
    "responsive_web_graphql_exclude_directive_enabled": True,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "responsive_web_grok_community_note_auto_translation_is_enabled": False,
    "responsive_web_graphql_timeline_navigation_enabled": True,
    "responsive_web_grok_imagine_annotation_enabled": False,
    "responsive_web_media_download_video_enabled": False,
    "responsive_web_profile_redirect_enabled": True,
    "responsive_web_twitter_article_tweet_consumption_enabled": True,
    "rweb_tipjar_consumption_enabled": True,
    "rweb_video_timestamps_enabled": True,
    "standardized_nudges_misinfo": True,
    "tweet_awards_web_tipping_enabled": False,
    "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
    "tweet_with_visibility_results_prefer_gql_media_interstitial_enabled": False,
    "tweetypie_unmention_optimization_enabled": True,
    "verified_phone_label_enabled": False,
    "view_counts_everywhere_api_enabled": True,
    "responsive_web_grok_analyze_button_fetch_trends_enabled": False,
    "premium_content_api_read_enabled": False,
    "profile_label_improvements_pcf_label_in_post_enabled": False,
    "responsive_web_grok_share_attachment_enabled": False,
    "responsive_web_grok_analyze_post_followups_enabled": False,
    "responsive_web_grok_image_annotation_enabled": False,
    "responsive_web_grok_analysis_button_from_backend": False,
    "responsive_web_jetfuel_frame": False,
    "rweb_video_screen_enabled": True,
    "responsive_web_grok_show_grok_translated_post": True,
}


def parse_created(s):
    try:
        return datetime.strptime(s, "%a %b %d %H:%M:%S %z %Y")
    except Exception:
        return None


def get_query_id():
    """指纹来源：twscrape 源码实时拉取 -> 兜底值"""
    try:
        r = requests.get("https://raw.githubusercontent.com/vladkens/twscrape/main/twscrape/api.py",
                         timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        m = re.search(r'OP_SearchTimeline\s*=\s*"([0-9a-zA-Z_-]+?)/SearchTimeline"', r.text)
        if m:
            return m.group(1), "twscrape-live"
    except Exception as e:
        print("twscrape指纹拉取失败:", e, flush=True)
    return OP_FALLBACK, "fallback"


def extract_tweets(payload):
    """从 SearchTimeline 响应提取 (tweets, bottom_cursor)"""
    tweets, cursor = [], None
    st = ((payload.get("data") or {}).get("search_by_raw_query") or {}).get("search_timeline") or {}
    instrs = (st.get("timeline") or {}).get("instructions") or []
    for ins in instrs:
        if ins.get("type") not in ("TimelineAddEntries", "TimelineAddToModule"):
            continue
        for entry in ins.get("entries") or []:
            eid = str(entry.get("entryId", ""))
            if eid.startswith("cursor-bottom"):
                cursor = entry.get("content", {}).get("value")
                continue
            content = entry.get("content", {})
            items = []
            if eid.startswith("tweet-") and content.get("itemContent"):
                items = [content]
            elif content.get("entryType") == "TimelineTimelineModule" or content.get("items"):
                items = content.get("items") or []
            for it in items:
                ic = it.get("itemContent") or {}
                if ic.get("entryType") != "TimelineTimelineItem":
                    continue
                tw = ic.get("tweet_results", {}).get("result", {})
                if tw.get("__typename") == "TweetWithVisibilityResults":
                    tw = tw.get("tweet") or {}
                leg = tw.get("legacy") or {}
                tid = leg.get("id_str")
                if not tid:
                    continue
                tweets.append({
                    "id": tid,
                    "created_at": parse_created(leg.get("created_at", "")),
                    "text": leg.get("full_text", ""),
                    "likes": leg.get("favorite_count", 0),
                    "rts": leg.get("retweet_count", 0),
                    "replies": leg.get("reply_count", 0),
                    "is_retweet": leg.get("full_text", "").startswith("RT @"),
                    "url": f"https://x.com/{HANDLE}/status/{tid}",
                })
    return tweets, cursor


def search_page(h, op, raw_query, cursor):
    variables = {"rawQuery": raw_query, "count": 20, "product": "Latest",
                 "querySource": "typed_query"}
    if cursor:
        variables["cursor"] = cursor
    r = monitor._x_get(
        f"{GQL_URL}/{op}/SearchTimeline",
        h,
        {"variables": json.dumps(variables),
         "features": json.dumps(FEATURES),
         "fieldToggles": json.dumps({"withArticleRichContentState": False})},
    )
    return r


def main():
    auth = monitor.secret("TWITTER_AUTH_TOKEN", "")
    if not auth:
        print("TWITTER_AUTH_TOKEN 未配置")
        sys.exit(1)

    op, src = get_query_id()
    print("SearchTimeline op:", op, "来源:", src, flush=True)

    h = monitor._x_headers(auth)
    raw_query = f"from:{HANDLE} since:{SINCE} until:{UNTIL}"
    print("query:", raw_query, flush=True)

    seen, kept, cursor = set(), [], None
    empty_pages = 0
    for page in range(1, MAX_PAGES + 1):
        for attempt in range(8):
            r = search_page(h, op, raw_query, cursor)
            if r.status_code == 429:
                print(f"page {page} attempt {attempt}: 429 限流, sleep 45s", flush=True)
                time.sleep(45)
                continue
            break
        else:
            print("连续限流，提前收工保存已有数据", flush=True)
            break

        if r.status_code != 200:
            print(f"page {page}: HTTP {r.status_code}, body[:300]={r.text[:300]}", flush=True)
            if r.status_code in (401, 403, 404):
                sys.exit(1)
            time.sleep(10)
            empty_pages += 1
            if empty_pages >= 5:
                break
            continue

        try:
            payload = r.json()
        except Exception:
            print(f"page {page}: 非JSON响应", flush=True)
            empty_pages += 1
            if empty_pages >= 5:
                break
            time.sleep(5)
            continue

        tweets, cursor = extract_tweets(payload)
        new = [t for t in tweets if t["id"] not in seen]
        for t in new:
            seen.add(t["id"])
            kept.append(t)

        times = [t["created_at"] for t in tweets if t["created_at"]]
        rng = (f"{min(times).astimezone(timezone.utc):%Y-%m-%d}~"
               f"{max(times).astimezone(timezone.utc):%Y-%m-%d}") if times else "空"
        print(f"page {page}: 新增{len(new)} 累计{len(kept)} 范围{rng} cursor={'有' if cursor else '无'}", flush=True)

        if not new:
            empty_pages += 1
            if empty_pages >= 3:
                break
        else:
            empty_pages = 0
        if not cursor:
            break
        time.sleep(PAGE_SLEEP)

    kept.sort(key=lambda t: (t["created_at"] or datetime(1970, 1, 1, tzinfo=timezone.utc), t["id"]))
    for t in kept:
        if t["created_at"]:
            t["created_at"] = t["created_at"].astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    os.makedirs("data", exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump({"handle": HANDLE, "query": raw_query,
                   "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                   "count": len(kept), "tweets": kept}, f, ensure_ascii=False, indent=1)
    print(f"完成: {len(kept)} 条 -> {OUT_PATH}")
    if not kept:
        sys.exit(1)


if __name__ == "__main__":
    main()
