# -*- coding: utf-8 -*-
"""
抓取 @thankUcrypto 2025-01-01 以来的全部推文（UserTweets 深翻页，断点续跑）。
- 状态存 data/aoying_history_state.json（cursor + 已收集推文），每轮提交回仓库
- 单轮软停 22 分钟（exit 42），由 workflow 自动再触发下一轮
- 翻到 2025-01-01 边界后导出 data/aoying_history_tweets.json（exit 0）
复用 monitor.py 的 X GraphQL 通道（auth_token 走 Secrets：TWITTER_AUTH_TOKEN）。
"""
import json
import os
import sys
import time
from datetime import datetime, timezone

import monitor

HANDLE = "thankUcrypto"
SINCE = datetime(2025, 1, 1, tzinfo=timezone.utc)
STATE_PATH = os.path.join("data", "aoying_history_state.json")
OUT_PATH = os.path.join("data", "aoying_history_tweets.json")
SOFT_STOP_SEC = 22 * 60
PAGE_SLEEP = 1.0
START = time.time()


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
    os.replace(tmp, STATE_PATH)


def export(st):
    kept = sorted(st["tweets"], key=lambda t: (t.get("created_at") or "", t["id"]))
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump({"handle": HANDLE, "channel": "x_graphql_usertweets",
                   "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                   "count": len(kept), "tweets": kept}, f, ensure_ascii=False, indent=1)


def get_uid(h, qids):
    r = monitor._x_get(
        f"https://x.com/i/api/graphql/{qids['UserByScreenName']}/UserByScreenName",
        h, {"variables": json.dumps({"screen_name": HANDLE, "withGrokTranslatedBio": False}),
            "features": json.dumps(monitor._X_FEATURES_USER)})
    r.raise_for_status()
    return r.json()["data"]["user"]["result"]["rest_id"]


def extract(payload):
    """UserTweets 响应 -> (tweets, bottom_cursor)"""
    tweets, cursor = [], None
    data = payload.get("data", {}).get("user", {}).get("result", {})
    instrs = (data.get("timeline_v2") or data.get("timeline") or {}).get("timeline", {}).get("instructions", [])
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
            elif eid.startswith("profile-conversation-") or content.get("items"):
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

    st = load_state()
    seen = {t["id"] for t in st["tweets"]}
    print(f"载入状态: 已存{len(seen)}条 cursor={'有' if st['cursor'] else '无'} done={st['done']}", flush=True)
    if st.get("done"):
        export(st)
        print("已完成，仅重新导出")
        return

    qids = monitor._x_query_ids()
    h = monitor._x_headers(auth)
    uid = get_uid(h, qids)
    print("userId:", uid, flush=True)

    cursor = st["cursor"]
    pages = 0
    while time.time() - START < SOFT_STOP_SEC:
        for attempt in range(10):
            variables = {"userId": uid, "count": 20, "includePromotedContent": False,
                         "withQuickPromoteEligibilityTweetField": True, "withVoice": False}
            if cursor:
                variables["cursor"] = cursor
            r = monitor._x_get(
                f"https://x.com/i/api/graphql/{qids['UserTweets']}/UserTweets",
                h, {"variables": json.dumps(variables),
                    "features": json.dumps(monitor._X_FEATURES_FEED)})
            if r.status_code == 429:
                print(f"page {pages+1} attempt {attempt}: 429, sleep 30s", flush=True)
                time.sleep(30)
                continue
            break
        else:
            print("持续限流，本轮软停", flush=True)
            break

        if r.status_code != 200:
            print(f"page {pages+1}: HTTP {r.status_code} {r.text[:200]}", flush=True)
            if r.status_code in (401, 403, 404):
                save_state(st)
                export(st)
                sys.exit(1)
            time.sleep(10)
            continue

        try:
            payload = r.json()
        except Exception:
            print(f"page {pages+1}: 非JSON", flush=True)
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

        if len(times) >= 5 and max(times) < SINCE:
            st["done"] = True
            st["cursor"] = None
            save_state(st)
            export(st)
            print("已翻到2025-01-01边界，抓取完成", flush=True)
            return
        if not cursor:
            st["done"] = True
            st["cursor"] = None
            save_state(st)
            export(st)
            print("无更多cursor，抓取完成", flush=True)
            return
        st["cursor"] = cursor
        time.sleep(PAGE_SLEEP)

    st["cursor"] = cursor
    save_state(st)
    export(st)
    mins = (time.time() - START) / 60
    print(f"本轮软停({mins:.0f}分钟, {pages}页, 累计{len(st['tweets'])}条)，需要续跑", flush=True)
    sys.exit(42)


if __name__ == "__main__":
    main()
