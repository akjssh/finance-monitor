# -*- coding: utf-8 -*-
"""
用 SearchTimeline 抓 @thankUcrypto 2025 全年推文（区间锁定，页数少）。
查询: from:thankUcrypto since:2025-01-01 until:2026-01-01
断点续跑: state 存 data/aoying_2025_state.json，导出 data/aoying_2025_tweets.json
单轮软停 18 分钟（exit 42），由 workflow 自动续触发下一轮。
复用 monitor.py 的 X GraphQL 通道（auth_token 走 Secrets：TWITTER_AUTH_TOKEN）。
"""
import json
import os
import sys
import time
from datetime import datetime, timezone

import monitor

HANDLE = "thankUcrypto"
RAW_QUERY = f"from:{HANDLE} since:2025-01-01 until:2026-01-01"
STATE_PATH = os.path.join("data", "aoying_2025_state.json")
OUT_PATH = os.path.join("data", "aoying_2025_tweets.json")
SOFT_STOP_SEC = 18 * 60
PAGE_SLEEP = 1.5
START = time.time()

SEARCH_QID = "hyPfJYJ_XAtDYoslQc-Rgg"

SEARCH_FEATURES = {
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


def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {"cursor": None, "done": False, "tweets": []}


def save_state(st):
    os.makedirs("data", exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        kept = sorted(st["tweets"], key=lambda t: (t.get("created_at") or "", t["id"]))
        json.dump({"handle": HANDLE, "query": RAW_QUERY,
                   "channel": "x_graphql_searchtimeline",
                   "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                   "count": len(kept), "tweets": kept}, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE_PATH)


def extract(payload):
    """SearchTimeline 响应 -> (tweets, bottom_cursor)"""
    tweets, cursor = [], None
    search = payload.get("data", {}).get("search_by_raw_query", {}).get("search_timeline", {})
    instrs = search.get("timeline", {}).get("instructions", [])

    for ins in instrs:
        entries = ins.get("entries") or []
        if not entries and ins.get("entry"):
            entries = [ins["entry"]]
        for entry in entries:
            eid = str(entry.get("entryId", ""))
            if "cursor-" in eid or "cursor" in eid:
                content = entry.get("content", {})
                c = content.get("value")
                if not c:
                    ic = content.get("itemContent") or {}
                    c = ic.get("value") or ic.get("cursorValue")
                if c:
                    cursor = c
                continue
            content = entry.get("content", {})
            items = []
            if eid.startswith("tweet-"):
                ic = content.get("itemContent")
                items = [{"itemContent": ic}] if ic else [content]
            elif content.get("items"):
                for it in content.get("items", []):
                    ic = it.get("itemContent")
                    if ic:
                        items.append({"itemContent": ic})
            elif content.get("itemContent"):
                items = [{"itemContent": content["itemContent"]}]

            for it in items:
                ic = it.get("itemContent") or {}
                tw = ic.get("tweet_results", {}).get("result", {})
                if not tw:
                    tw = ic.get("tweet") or {}
                if not tw:
                    continue
                if tw.get("__typename") == "TweetWithVisibilityResults":
                    tw = tw.get("tweet") or {}
                leg = tw.get("legacy") or {}
                tid = leg.get("id_str") or tw.get("rest_id")
                if not tid:
                    continue
                created = parse_created(leg.get("created_at", ""))
                tweets.append({
                    "id": tid,
                    "created_at": created,
                    "text": leg.get("full_text", ""),
                    "likes": leg.get("favorite_count", 0),
                    "rts": leg.get("retweet_count", 0),
                    "replies": leg.get("reply_count", 0),
                    "is_retweet": leg.get("full_text", "").startswith("RT @"),
                    "url": f"https://x.com/{HANDLE}/status/{tid}",
                })
    return tweets, cursor


def main():
    auth = monitor.secret("TWITTER_AUTH_TOKEN", "")
    if not auth:
        print("TWITTER_AUTH_TOKEN 未配置")
        sys.exit(1)

    st = load_state()
    seen = {t["id"] for t in st["tweets"]}
    print(f"载入状态: 已存{len(seen)}条 cursor={'有' if st['cursor'] else '无'} done={st['done']}", flush=True)
    if st.get("done"):
        print("已完成")
        return

    h = monitor._x_headers(auth)
    cursor = st["cursor"]
    pages = 0
    while time.time() - START < SOFT_STOP_SEC:
        for attempt in range(10):
            variables = {"rawQuery": RAW_QUERY, "count": 20,
                         "querySource": "typed_query", "product": "Latest"}
            if cursor:
                variables["cursor"] = cursor
            r = monitor._x_get(
                f"https://x.com/i/api/graphql/{SEARCH_QID}/SearchTimeline",
                h, {"variables": json.dumps(variables),
                    "features": json.dumps(SEARCH_FEATURES)})
            if r.status_code == 429:
                print(f"page {pages+1} attempt {attempt}: 429, sleep 30s", flush=True)
                time.sleep(30)
                continue
            break
        else:
            print("持续限流，本轮软停", flush=True)
            break

        if r.status_code != 200:
            print(f"page {pages+1}: HTTP {r.status_code} {r.text[:300]}", flush=True)
            if r.status_code in (401, 403, 404):
                save_state(st)
                sys.exit(1)
            time.sleep(10)
            continue

        try:
            payload = r.json()
        except Exception:
            print(f"page {pages+1}: 非JSON, body[:300]={r.text[:300]}", flush=True)
            time.sleep(5)
            continue

        tweets, cursor = extract(payload)
        pages += 1
        new = 0
        for t in tweets:
            if t["id"] in seen:
                continue
            seen.add(t["id"])
            new += 1
            st["tweets"].append({
                "id": t["id"], "created_at": (t["created_at"].astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                                              if t["created_at"] else None),
                "text": t["text"], "likes": t["likes"], "rts": t["rts"],
                "replies": t["replies"], "is_retweet": t["is_retweet"], "url": t["url"],
            })
        times = [t["created_at"] for t in tweets if t["created_at"]]
        rng = (f"{min(times).astimezone(timezone.utc):%Y-%m-%d}~{max(times).astimezone(timezone.utc):%Y-%m-%d}"
               if times else "空")
        print(f"page {pages}: 新增{new} 累计{len(st['tweets'])} 范围{rng} cursor={'有' if cursor else '无'}", flush=True)

        if not cursor:
            st["done"] = True
            st["cursor"] = None
            save_state(st)
            print("无更多cursor，抓取完成", flush=True)
            return
        st["cursor"] = cursor
        time.sleep(PAGE_SLEEP)

    st["cursor"] = cursor
    save_state(st)
    mins = (time.time() - START) / 60
    print(f"本轮软停({mins:.0f}分钟, {pages}页, 累计{len(st['tweets'])}条)，需要续跑", flush=True)
    sys.exit(42)


if __name__ == "__main__":
    main()