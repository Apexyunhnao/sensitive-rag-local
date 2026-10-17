#!/usr/bin/env python3
"""Claude Code 花费监控 —— 直接读本地会话 JSONL 的真实 token 用量。

用法：
  python cost_watch.py                  # 汇总今天所有会话
  python cost_watch.py --since 20:00    # 只看今天 20:00 之后的会话
  python cost_watch.py --watch 15       # 每 15 秒刷新一次（Ctrl+C 退出）
  python cost_watch.py --rate-in 4.95 --rate-out 24 --rate-cache 1.67
                                        # 手动指定单价（$/百万 token），校准后设这里

单价默认按 xcdw 站 claude-opus-5-5 的 model_ratio 4.95 估算，
**校准方法**：跑一次已知 token 的请求，对比站点余额差值，反算真实单价。
"""
import argparse
import glob
import json
import os
import sys
import time
from datetime import datetime, timedelta

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PROJECTS = os.path.expanduser("~/.claude/projects")
STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cost_state.json")

# 实测校准值（2026-10-17）：充值 ¥20+赠¥5，29 轮 1,966,694 input 只扣 ¥2
# ⇒ 实际约 ¥1.02/百万 input，折合 $0.14/M。站方标价 model_ratio=4.95 有约 4.9 折
# 不要再拿 /api/pricing 的 ratio 直接当单价，会算高 35 倍。
DEFAULT_RATES = {"input": 0.14, "output": 0.68, "cache_read": 0.05, "cache_create": 0.14}
USD_CNY = 7.2


def scan(since_ts=None):
    """返回 {session_id: {tokens, first_ts, last_ts, model, path}}"""
    out = {}
    for path in glob.glob(os.path.join(PROJECTS, "**", "*.jsonl"), recursive=True):
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        if since_ts and mtime < since_ts - 300:
            continue
        sid = os.path.basename(path)[:8]
        acc = out.setdefault(
            sid,
            {"in": 0, "out": 0, "cr": 0, "cc": 0, "calls": 0, "first": None, "last": None,
             "model": "", "path": path},
        )
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    try:
                        d = json.loads(line)
                    except Exception:
                        continue
                    msg = d.get("message") or {}
                    u = msg.get("usage") or d.get("usage") or {}
                    if not u:
                        continue
                    ts = d.get("timestamp", "")
                    if since_ts and ts:
                        try:
                            t = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
                            if t < since_ts:
                                continue
                        except Exception:
                            pass
                    acc["calls"] += 1
                    acc["in"] += u.get("input_tokens") or 0
                    acc["out"] += u.get("output_tokens") or 0
                    acc["cr"] += u.get("cache_read_input_tokens") or 0
                    acc["cc"] += u.get("cache_creation_input_tokens") or 0
                    if msg.get("model"):
                        acc["model"] = msg["model"]
                    if ts:
                        if not acc["first"] or ts < acc["first"]:
                            acc["first"] = ts
                        if not acc["last"] or ts > acc["last"]:
                            acc["last"] = ts
        except OSError:
            continue
    return out


def money(acc, rates):
    d = (
        acc["in"] / 1e6 * rates["input"]
        + acc["out"] / 1e6 * rates["output"]
        + acc["cr"] / 1e6 * rates["cache_read"]
        + acc["cc"] / 1e6 * rates["cache_create"]
    )
    return d


def report(rates, since_ts, label):
    data = scan(since_ts)
    data = {k: v for k, v in data.items() if v["calls"]}
    rows = sorted(data.items(), key=lambda kv: money(kv[1], rates), reverse=True)
    total = sum(money(v, rates) for _, v in data.items())
    tin = sum(v["in"] for v in data.values())
    tout = sum(v["out"] for v in data.values())
    tcr = sum(v["cr"] for v in data.values())
    tcc = sum(v["cc"] for v in data.values())

    print(f"\n=== {label}  {datetime.now():%H:%M:%S} ===")
    print(f"{'会话':<10}{'轮次':>5}{'输入':>12}{'输出':>8}{'缓存读':>12}{'估算$':>9}")
    for sid, v in rows[:8]:
        print(
            f"{sid:<10}{v['calls']:>5}{v['in']:>12,}{v['out']:>8,}{v['cr']:>12,}"
            f"{money(v, rates):>9.3f}"
        )
    print("-" * 58)
    print(
        f"{'合计':<10}{sum(v['calls'] for v in data.values()):>5}{tin:>12,}{tout:>8,}"
        f"{tcr:>12,}{total:>9.3f}"
    )
    print(f"约 ¥{total * USD_CNY:.2f} ｜ 缓存命中率 {tcr / max(tin + tcr, 1) * 100:.0f}%")
    if tcr == 0 and tin > 50000:
        print("⚠ 缓存读为 0 —— 这个中转不支持 prompt caching，每一轮都是全价重算")
    return total


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", help="HH:MM 只看今天该时间之后")
    ap.add_argument("--watch", type=int, metavar="SEC", help="每 N 秒刷新")
    ap.add_argument("--rate-in", type=float, default=DEFAULT_RATES["input"])
    ap.add_argument("--rate-out", type=float, default=DEFAULT_RATES["output"])
    ap.add_argument("--rate-cache", type=float, default=DEFAULT_RATES["cache_read"])
    ap.add_argument("--all", action="store_true", help="不限时间，扫全部历史")
    a = ap.parse_args()

    rates = {"input": a.rate_in, "output": a.rate_out,
             "cache_read": a.rate_cache, "cache_create": a.rate_in}

    since_ts = None
    label = "全部历史"
    if not a.all:
        now = datetime.now()
        if a.since:
            h, m = map(int, a.since.split(":"))
            since_ts = now.replace(hour=h, minute=m, second=0, microsecond=0).timestamp()
            label = f"今天 {a.since} 之后"
        else:
            since_ts = (now - timedelta(hours=2)).timestamp()
            label = "最近 2 小时"
    if since_ts and since_ts > time.time():
        since_ts -= 86400  # 跨零点

    if not a.watch:
        report(rates, since_ts, label)
        return
    try:
        while True:
            os.system("cls" if os.name == "nt" else "clear")
            report(rates, since_ts, label)
            time.sleep(a.watch)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
