# -*- coding: utf-8 -*-
"""
一次性任务：用 SearchTimeline 抓取 @thankUcrypto 2025 全年推文。
查询区间可用环境变量覆盖：X_SINCE / X_UNTIL（默认 2025-01-01 / 2026-01-01）。
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
SINCE = os.environ.get("X_SINCE", "2025-01-01")
UNTIL = os.environ.get("X_UNTIL", "2026-01-01")
MAX_PAGES = 500
PAGE_SLEEP = 1.0
OUT_PATH = os.path.join("data", "aoying_history_tweets.json")


def parse_created(s):
    try:
        return datetime.strptime(s, "%a %b %d %H:%M:%S %z %Y")
    except Exception:
        return None


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


def search_page(h, qid, raw_query, cursor):
    variables = {"rawQuery": raw_query, "count": 100, "querySource": "typed_query", "product": "Latest"}
    if cursor:
        variables["cursor"] = cursor
    r = monitor._x_get(
        f"https://x.com/i/api/graphql/{qid}/SearchTimeline",
        h, {"variables": json.dumps(variables), "features": json.dumps(monitor._X_FEATURES_FEED)},
    )
    return r


def main():
    auth = monitor.secret("TWITTER_AUTH_TOKEN", "")
    if not auth:
        print("TWITTER_AUTH_TOKEN 未配置")
        sys.exit(1)

    qid = None
    try:
        import requests as _rq
        rr = _rq.get("https://cdn.jsdelivr.net/gh/fa0311/TwitterInternalAPIDocument@master/docs/json/API.json", timeout=15)
        qid = rr.json().get("graphql", {}).get("SearchTimeline", {}).get("queryId")
    except Exception as e:
        print("动态获取queryId失败:", e, flush=True)
    if not qid:
        qid = "KPSo2_UWdOMpPJwjhfT1Qg"  # 兜底指纹
        print("使用兜底queryId:", qid, flush=True)
    print("SearchTimeline queryId:", qid, flush=True)

    h = monitor._x_headers(auth)
    raw_query = f"from:{HANDLE} since:{SINCE} until:{UNTIL}"
    print("query:", raw_query, flush=True)

    seen, kept, cursor = set(), [], None
    empty_pages = 0
    for page in range(1, MAX_PAGES + 1):
        for attempt in range(8):
            r = search_page(h, qid, raw_query, cursor)
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
