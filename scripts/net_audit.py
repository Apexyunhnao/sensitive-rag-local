"""数据不出网审计 —— 证明敏感文档处理全程没有外部网络连接。

做法：跑 demo 的同时高频采样本机所有 TCP 连接，
凡是非回环地址（127.0.0.1 / ::1）且属于 python / ollama 进程的，一律记为违规。

用法：
    python scripts/net_audit.py python scripts/demo.py
    python scripts/net_audit.py --report-only      # 只看当前快照

退出码 0 = 无外部连接；2 = 发现外部连接。
"""
import json
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
REPORT = ROOT / "out" / "net_audit.json"
WATCH_NAMES = ("python", "python3", "ollama", "ollama_llama_server", "llama-server")


def is_loopback(ip):
    if not ip:
        return True
    if ip in ("127.0.0.1", "::1", "0.0.0.0", "::"):
        return True
    return ip.startswith("127.")


def sampler(stop_evt, hits, samples):
    import psutil

    while not stop_evt.is_set():
        try:
            for c in psutil.net_connections(kind="tcp"):
                samples[0] += 1
                laddr, raddr = c.laddr, c.raddr
                if not raddr:
                    continue
                rip = raddr.ip if hasattr(raddr, "ip") else raddr[0]
                if is_loopback(rip):
                    continue
                pid = c.pid
                name = ""
                if pid:
                    try:
                        name = psutil.Process(pid).name().lower()
                    except Exception:
                        name = ""
                if any(w in name for w in WATCH_NAMES):
                    hits.append(
                        {
                            "ts": time.strftime("%H:%M:%S"),
                            "pid": pid,
                            "proc": name,
                            "remote": f"{rip}:{raddr.port if hasattr(raddr,'port') else raddr[1]}",
                            "status": c.status,
                        }
                    )
        except Exception:
            pass
        time.sleep(0.2)


def main():
    args = sys.argv[1:]
    report_only = "--report-only" in args
    if report_only:
        args = [a for a in args if a != "--report-only"]

    if report_only or not args:
        import psutil

        ext = []
        for c in psutil.net_connections(kind="tcp"):
            if c.raddr:
                rip = c.raddr.ip if hasattr(c.raddr, "ip") else c.raddr[0]
                if not is_loopback(rip):
                    try:
                        nm = psutil.Process(c.pid).name() if c.pid else "?"
                    except Exception:
                        nm = "?"
                    if any(w in nm.lower() for w in WATCH_NAMES):
                        ext.append({"pid": c.pid, "proc": nm, "remote": f"{rip}:{c.raddr.port}"})
        print(f"快照：命中 {len(ext)} 条外部连接")
        for e in ext:
            print(" ", e)
        sys.exit(0 if not ext else 2)

    hits, samples = [], [0]
    stop = threading.Event()
    t = threading.Thread(target=sampler, args=(stop, hits, samples), daemon=True)
    t.start()

    print(f"[audit] 开始采样，执行：{' '.join(args)}\n" + "-" * 60)
    t0 = time.time()
    rc = subprocess.call(args, cwd=str(ROOT))
    dt = time.time() - t0
    stop.set()
    t.join(timeout=2)

    print("-" * 60)
    uniq = {(h["proc"], h["remote"]) for h in hits}
    print(f"[audit] 运行 {dt:.0f}s，采样 {samples[0]} 次连接检查")
    print(f"[audit] 外部连接：{len(uniq)} 种")

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(
        json.dumps(
            {
                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "command": args,
                "duration_s": round(dt, 1),
                "samples": samples[0],
                "external_connections": sorted(uniq),
                "raw_hits": hits[:200],
                "verdict": "PASS" if not uniq else "FAIL",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"[audit] 报告：{REPORT}")

    if uniq:
        print("\n⚠ 发现外部连接（数据可能出网）：")
        for p, r in sorted(uniq):
            print(f"   {p} -> {r}")
        sys.exit(2)
    print("\n✓ 全程只有本机回环连接，无外部网络通信")
    sys.exit(0)


if __name__ == "__main__":
    main()
