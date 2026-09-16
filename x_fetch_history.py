# -*- coding: utf-8 -*-
"""
抓取 @thankUcrypto 2025 全年推文。
主通道：twitterapi.io 高级搜索（ Secrets: TWITTERAPI_IO_KEY，不受 X 对云IP的风控影响）。
输出 data/aoying_history_tweets.json（按时间升序），由 workflow 提交回仓库。
"""
import json
import os
import sys
import time
from datetime import datetime, timezone

import requests

HANDLE = "thankUcrypto"
SINCE = os.environ.get("X_SINCE", "2025-01-01")
UNTIL = os.environ.get("X_UNTIL", "2026-01-01")
MAX_PAGES = 400
PAGE_SLEEP = 1.0
OUT_PATH = os.path.join("data", "aoying_history_tweets.json")


def parse_created(s):
    fmts = ("%a %b %d %H:%M:%S %z %Y", "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ")
    for f in fmts:
        try:
            return datetime.strptime(s, f)
        except Exception:
            continue
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def main():
    key = os.environ.get("TWITTERAPI_IO_KEY", "")
    if not key:
        print("TWITTERAPI_IO_KEY 未配置（Secrets）")
        sys.exit(1)

    h = {"X-API-Key": key, "User-Agent": "finance-monitor"}
    q = f"from:{HANDLE} since:{SINCE} until:{UNTIL}"
    url = "https://api.twitterapi.io/twitter/tweet/advanced_search"

    seen, kept, cursor = set(), [], None
    for page in range(1, MAX_PAGES + 1):
        params = {"query": q, "queryType": "Latest"}
        if cursor:
            params["cursor"] = cursor
        r = requests.get(url, params=params, headers=h, timeout=30)
        if r.status_code == 429:
            print(f"page {page}: 429, sleep 30s", flush=True)
            time.sleep(30)
            continue
        if r.status_code != 200:
            print(f"page {page}: HTTP {r.status_code}, {r.text[:300]}", flush=True)
            if r.status_code in (401, 403):
                sys.exit(1)
            time.sleep(10)
            continue
        d = r.json()
        tweets = d.get("tweets") or []
        new = 0
        for t in tweets:
            tid = str(t.get("id") or t.get("id_str") or "")
            if not tid or tid in seen:
                continue
            seen.add(tid)
            new += 1
            kept.append({
                "id": tid,
                "created_at": (parse_created(t.get("createdAt") or "") or datetime(1970, 1, 1, tzinfo=timezone.utc)),
                "text": t.get("text", ""),
                "likes": t.get("likeCount", 0),
                "rts": t.get("retweetCount", 0),
                "replies": t.get("replyCount", 0),
                "is_retweet": (t.get("text", "").startswith("RT @")) or bool(t.get("retweeted_tweet")),
                "url": f"https://x.com/{HANDLE}/status/{tid}",
            })
        cur2 = d.get("next_cursor")
        times = [t["created_at"] for t in kept if t.get("created_at")]
        rng = (f"{min(times).astimezone(timezone.utc):%Y-%m-%d}~"
               f"{max(times).astimezone(timezone.utc):%Y-%m-%d}") if times else "空"
        print(f"page {page}: 新增{new} 累计{len(kept)} 范围{rng} cursor={'有' if cur2 else '无'}", flush=True)
        if not cur2:
            break
        cursor = cur2
        time.sleep(PAGE_SLEEP)

    kept.sort(key=lambda t: (t["created_at"], t["id"]))
    for t in kept:
        if isinstance(t["created_at"], datetime):
            t["created_at"] = t["created_at"].astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    os.makedirs("data", exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump({"handle": HANDLE, "query": q, "channel": "twitterapi_io",
                   "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                   "count": len(kept), "tweets": kept}, f, ensure_ascii=False, indent=1)
    print(f"完成: {len(kept)} 条 -> {OUT_PATH}")
    if not kept:
        sys.exit(1)


if __name__ == "__main__":
    main()
