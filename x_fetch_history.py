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
DEBUG_PATH = os.path.join("data", "aoying_debug_response.json")
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
    """UserTweets 响应 -> (tweets, bottom_cursor)
    健壮提取：遍历所有 instruction 类型，尝试多种 entry/tweet 结构。
    """
    tweets, cursor = [], None
    data = payload.get("data", {}).get("user", {}).get("result", {})

    # 尝试 timeline_v2 -> timeline -> instructions 和 timeline -> instructions
    timeline_obj = data.get("timeline_v2") or data.get("timeline") or {}
    instrs = timeline_obj.get("timeline", {}).get("instructions", [])
    # 也尝试直接在 data 下找 instructions
    if not instrs:
        instrs = timeline_obj.get("instructions", [])

    for ins in instrs:
        ins_type = ins.get("type", "")
        # 处理多种 instruction 类型
        entries = ins.get("entries") or []
        if not entries and ins.get("entry"):
            entries = [ins["entry"]]

        for entry in entries:
            eid = str(entry.get("entryId", ""))

            # cursor-bottom / cursor-top / cursor-showmore
            if "cursor-" in eid or "cursor" in eid:
                content = entry.get("content", {})
                # cursor value 可能在 content.value 或 content.itemContent.value
                c = content.get("value")
                if not c:
                    ic = content.get("itemContent") or {}
                    c = ic.get("value") or ic.get("cursorValue")
                if c:
                    cursor = c
                continue

            content = entry.get("content", {})
            items = []

            # 标准 tweet entry
            if eid.startswith("tweet-"):
                ic = content.get("itemContent")
                if ic:
                    items = [{"itemContent": ic}]
                else:
                    # 有时 tweet 内容直接在 content 里
                    items = [content]

            # conversation entry（含多个 tweet）
            elif eid.startswith("profile-conversation-") or content.get("items"):
                for it in (content.get("items") or []):
                    ic = it.get("itemContent")
                    if ic:
                        items.append({"itemContent": ic})

            # 其他可能包含 tweet 的 entry
            elif content.get("itemContent"):
                items = [{"itemContent": content["itemContent"]}]

            for it in items:
                ic = it.get("itemContent") or {}
                # 不限制 entryType，尝试所有可能的 tweet 结构
                tw = ic.get("tweet_results", {}).get("result", {})
                if not tw:
                    # 也尝试 ic.tweet 直接
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
        export(st)
        print("已完成，仅重新导出")
        return

    qids = monitor._x_query_ids()
    print(f"queryIds: {qids}", flush=True)
    h = monitor._x_headers(auth)
    uid = get_uid(h, qids)
    print("userId:", uid, flush=True)

    cursor = st["cursor"]
    pages = 0
    debug_saved = False
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
            print(f"page {pages+1}: HTTP {r.status_code} {r.text[:300]}", flush=True)
            if r.status_code in (401, 403, 404):
                save_state(st)
                export(st)
                sys.exit(1)
            time.sleep(10)
            continue

        try:
            payload = r.json()
        except Exception:
            print(f"page {pages+1}: 非JSON, body[:300]={r.text[:300]}", flush=True)
            time.sleep(5)
            continue

        # 第一页保存原始响应用于调试
        if not debug_saved:
            try:
                with open(DEBUG_PATH, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False, indent=1)
                print(f"已保存调试响应到 {DEBUG_PATH}", flush=True)
            except Exception as e:
                print(f"保存调试响应失败: {e}", flush=True)
            debug_saved = True

        tweets, cursor = extract(payload)
        pages += 1

        # 调试：第一页打印响应结构概要
        if pages == 1:
            try:
                data = payload.get("data", {}).get("user", {}).get("result", {})
                tl = data.get("timeline_v2") or data.get("timeline") or {}
                instrs = tl.get("timeline", {}).get("instructions", [])
                if not instrs:
                    instrs = tl.get("instructions", [])
                print(f"DEBUG: data keys={list(payload.get('data',{}).keys())}", flush=True)
                print(f"DEBUG: user.result keys={list(data.keys())}", flush=True)
                print(f"DEBUG: timeline keys={list(tl.keys())}", flush=True)
                print(f"DEBUG: instructions count={len(instrs)}", flush=True)
                for i, ins in enumerate(instrs):
                    entries = ins.get("entries") or []
                    print(f"DEBUG: instr[{i}] type={ins.get('type')} entries={len(entries)}", flush=True)
                    for j, e in enumerate(entries[:3]):
                        eid = e.get("entryId", "")
                        ct = e.get("content", {})
                        print(f"DEBUG:   entry[{j}] id={eid} content keys={list(ct.keys())}", flush=True)
                        ic = ct.get("itemContent") or {}
                        if ic:
                            print(f"DEBUG:     itemContent keys={list(ic.keys())}", flush=True)
                            tr = ic.get("tweet_results", {}).get("result", {})
                            if tr:
                                print(f"DEBUG:     tweet_results.result keys={list(tr.keys())}", flush=True)
                                print(f"DEBUG:     __typename={tr.get('__typename')}", flush=True)
                print(f"DEBUG: extract returned tweets={len(tweets)} cursor={'有' if cursor else '无'}", flush=True)
            except Exception as e:
                print(f"DEBUG打印异常: {e}", flush=True)

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