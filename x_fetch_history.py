# -*- coding: utf-8 -*-
"""
一次性任务：翻页抓取 @thankUcrypto 自 2025-01-01 以来的全部推文。
复用 monitor.py 的 X GraphQL 通道（auth_token 走 Secrets：TWITTER_AUTH_TOKEN）。
输出 data/aoying_history_tweets.json（按时间升序），由 workflow 提交回仓库。
"""
import json
import os
import sys
import time
from datetime import datetime, timezone

import monitor

HANDLE = "thankUcrypto"
SINCE = datetime(2025, 1, 1, tzinfo=timezone.utc)
MAX_PAGES = 400
PAGE_SLEEP = 0.8
OUT_PATH = os.path.join("data", "aoying_history_tweets.json")


def parse_created(s):
    try:
        return datetime.strptime(s, "%a %b %d %H:%M:%S %z %Y")
    except Exception:
        return None


def get_uid(h, auth):
    qids = monitor._x_query_ids()
    r = monitor._x_get(
        f"https://x.com/i/api/graphql/{qids['UserByScreenName']}/UserByScreenName",
        monitor._x_headers(auth),
        {"variables": json.dumps({"screen_name": h, "withGrokTranslatedBio": False}),
         "features": json.dumps(monitor._X_FEATURES_USER)},
    )
    r.raise_for_status()
    return r.json()["data"]["user"]["result"]["rest_id"]


def extract_entries(payload):
    """从 UserTweets 响应中提取 (tweets, bottom_cursor)"""
    tweets, cursor = [], None
    data = payload.get("data", {}).get("user", {}).get("result", {})
    instrs = (data.get("timeline_v2") or data.get("timeline") or {}).get("timeline", {}).get("instructions", [])
    for ins in instrs:
        if ins.get("type") == "TimelineAddEntries":
            for entry in ins.get("entries") or []:
                eid = str(entry.get("entryId", ""))
                if eid.startswith("cursor-bottom"):
                    cursor = entry.get("content", {}).get("value")
                    continue
                content = entry.get("content", {})
                items = []
                if eid.startswith("tweet-") and content.get("itemContent"):
                    items = [content]
                elif eid.startswith("profile-conversation-"):
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


def main():
    auth = monitor.secret("TWITTER_AUTH_TOKEN", "")
    if not auth:
        print("TWITTER_AUTH_TOKEN 未配置")
        sys.exit(1)

    qids = monitor._x_query_ids()
    print("queryIds:", json.dumps(qids))
    uid = get_uid(HANDLE, auth)
    print("userId:", uid)

    h = monitor._x_headers(auth)
    seen, kept, cursor = set(), [], None
    done = False
    for page in range(1, MAX_PAGES + 1):
        variables = {"userId": uid, "count": 20, "includePromotedContent": False,
                     "withQuickPromoteEligibilityTweetField": True, "withVoice": False}
        if cursor:
            variables["cursor"] = cursor
        r = monitor._x_get(
            f"https://x.com/i/api/graphql/{qids['UserTweets']}/UserTweets",
            h, {"variables": json.dumps(variables), "features": json.dumps(monitor._X_FEATURES_FEED)},
        )
        if r.status_code != 200:
            print(f"page {page}: HTTP {r.status_code}, body[:200]={r.text[:200]}")
            if r.status_code in (401, 403, 404):
                sys.exit(1)
            time.sleep(5)
            continue
        try:
            payload = r.json()
        except Exception:
            print(f"page {page}: 非JSON响应")
            time.sleep(5)
            continue

        tweets, cursor = extract_entries(payload)
        new = [t for t in tweets if t["id"] not in seen]
        for t in new:
            seen.add(t["id"])
            if t["created_at"] and t["created_at"] >= SINCE:
                kept.append(t)

        times = [t["created_at"] for t in tweets if t["created_at"]]
        earliest = min(times).astimezone(timezone.utc).strftime("%Y-%m-%d") if times else "?"
        latest = max(times).astimezone(timezone.utc).strftime("%Y-%m-%d") if times else "?"
        print(f"page {page}: 新增{len(new)} 累计{len(kept)} 范围{earliest}~{latest} cursor={'有' if cursor else '无'}", flush=True)

        if len(times) >= 5 and max(times) < SINCE:
            done = True
        if not cursor or done:
            break
        time.sleep(PAGE_SLEEP)

    kept.sort(key=lambda t: (t["created_at"] or SINCE, t["id"]))
    for t in kept:
        if t["created_at"]:
            t["created_at"] = t["created_at"].astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    os.makedirs("data", exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump({"handle": HANDLE, "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                   "count": len(kept), "tweets": kept}, f, ensure_ascii=False, indent=1)
    print(f"完成: {len(kept)} 条 -> {OUT_PATH}")
    if not kept:
        sys.exit(1)


if __name__ == "__main__":
    main()