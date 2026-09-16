# -*- coding: utf-8 -*-
"""
用 twscrape 库抓 @thankUcrypto 2025 全年推文（SearchTimeline + x-client-transaction-id 自动处理）。
查询: from:thankUcrypto since:2025-01-01 until:2026-01-01
断点续跑: state 存 data/aoying_2025_state.json，导出 data/aoying_2025_tweets.json
单轮软停 18 分钟（exit 42），由 workflow 自动续触发下一轮。
凭据: TWITTER_AUTH_TOKEN 走 Secrets，ct0 自生成（X 只要求 csrf 一致）。
"""
import asyncio
import json
import os
import sys
import time
import secrets as pysecrets
from datetime import datetime, timezone

HANDLE = "thankUcrypto"
RAW_QUERY = f"from:{HANDLE} since:2025-01-01 until:2026-01-01"
STATE_PATH = os.path.join("data", "aoying_2025_state.json")
OUT_PATH = os.path.join("data", "aoying_2025_tweets.json")
SOFT_STOP_SEC = 18 * 60
START = time.time()


def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {"done": False, "tweets": []}


def save_state(st):
    os.makedirs("data", exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        kept = sorted(st["tweets"], key=lambda t: (t.get("created_at") or "", t["id"]))
        json.dump({"handle": HANDLE, "query": RAW_QUERY,
                   "channel": "twscrape_search",
                   "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                   "count": len(kept), "tweets": kept}, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE_PATH)


async def main():
    from twscrape import AccountsPool, API

    auth = os.environ.get("TWITTER_AUTH_TOKEN", "")
    if not auth:
        print("TWITTER_AUTH_TOKEN 未配置")
        sys.exit(1)

    st = load_state()
    seen = {t["id"] for t in st["tweets"]}
    print(f"载入状态: 已存{len(seen)}条 done={st['done']}", flush=True)
    if st.get("done"):
        print("已完成")
        return

    pool = AccountsPool("accounts.db")
    ct0 = pysecrets.token_hex(16)
    cookies = f"auth_token={auth}; ct0={ct0}"
    try:
        await pool.add_account_cookies(HANDLE, cookies)
    except Exception as e:
        print(f"加账号失败: {e}", flush=True)

    api = API(pool)
    new = 0
    try:
        async for tw in api.search(RAW_QUERY, limit=10000):
            if time.time() - START > SOFT_STOP_SEC:
                print(f"软停时间到，已收集{len(st['tweets'])}条", flush=True)
                break
            tid = str(tw.id)
            if tid in seen:
                continue
            seen.add(tid)
            new += 1
            st["tweets"].append({
                "id": tid,
                "created_at": tw.date.strftime("%Y-%m-%dT%H:%M:%SZ") if tw.date else None,
                "text": tw.rawContent or "",
                "likes": tw.likeCount,
                "rts": tw.retweetCount,
                "replies": tw.replyCount,
                "is_retweet": bool(tw.retweetedTweet),
                "url": f"https://x.com/{HANDLE}/status/{tid}",
            })
            if new % 50 == 0:
                print(f"已收集{len(st['tweets'])}条...", flush=True)
                save_state(st)
    except Exception as e:
        print(f"抓取异常: {e}", flush=True)

    st["done"] = True
    save_state(st)
    mins = (time.time() - START) / 60
    print(f"完成: {len(st['tweets'])}条, 用时{mins:.0f}分钟", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
