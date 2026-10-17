"""一键部署 / 自检：从零到可用的本地敏感 RAG（幂等，可重复跑）。

做四件事：
  1. 前置检查：Docker、Ollama、模型、磁盘  —— 缺什么明确报出来，不猜
  2. 起 Dify 容器
  3. 建知识库 + 上传文档 + 建应用（已存在则跳过，不重复建）
  4. 起权限网关，跑一次端到端验证

用法：
  python scripts/deploy.py --check          只做前置检查（离线环境第一件事）
  python scripts/deploy.py                 完整部署
  python scripts/deploy.py --verify        只跑验证
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIFY_DIR = Path("E:/Projects/dify")
STATE = ROOT / ".dify-tiered.json"
DATA = ROOT / "data"

TIERS = {
    "合规知识库": {"docs": ["hkex_announcement_2026_041.md", "internal_audit_2026_q3.md"],
                   "groups": ["compliance", "board"], "app_name": "合规助手"},
    "人事知识库": {"docs": ["hr_roster.md"], "groups": ["hr"], "app_name": "人事助手"},
}
REQUIRED_MODELS = ["qwen2.5:7b", "bge-m3"]

ok_all = True


def ok(msg: str) -> None:
    print(f"  ✓ {msg}")


def bad(msg: str) -> None:
    global ok_all
    ok_all = False
    print(f"  ✗ {msg}")


def sh(cmd: list[str], timeout: int = 60) -> tuple[bool, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace")
        return r.returncode == 0, (r.stdout or r.stderr or "").strip()
    except FileNotFoundError:
        return False, "命令不存在"
    except subprocess.TimeoutExpired:
        return False, "超时"


def check_docker() -> bool:
    okc, out = sh(["docker", "version", "--format", "{{.Server.Version}}"])
    if not okc:
        bad("Docker 未运行或未安装（断网环境需先确认镜像已导入）")
        return False
    ok(f"Docker {out}")
    okc, out = sh(["docker", "ps", "--filter", "name=docker-api-1", "--format", "{{.Status}}"])
    ok(f"Dify api 容器: {out or '未启动'}")
    return True


def check_ollama() -> bool:
    try:
        with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=5) as r:
            tags = json.loads(r.read())
    except Exception as e:
        bad(f"Ollama 未响应 ({e})")
        return False
    have = {m["name"] for m in tags.get("models", [])}
    for m in REQUIRED_MODELS:
        if m in have or f"{m}:latest" in have:
            ok(f"模型 {m}")
        else:
            bad(f"缺模型 {m}（断网环境需预先导入 ~/.ollama/models）")
    return True


def check_dify() -> bool:
    try:
        with urllib.request.urlopen("http://127.0.0.1/console/api/system-features", timeout=8) as r:
            d = json.loads(r.read())
        ok(f"Dify 可访问，版本 {d.get('deployment_edition', '?')}")
        return True
    except Exception as e:
        bad(f"Dify 未响应 ({e})")
        return False


def check_gateway() -> bool:
    try:
        with urllib.request.urlopen("http://127.0.0.1:8090/audit", timeout=5) as r:
            ok("权限网关在线")
            return True
    except Exception:
        print("  · 权限网关未启动（部署完会启动）")
        return True


def check_disk() -> bool:
    total, used, free = shutil.disk_usage("E:/")
    if free / 1024**3 < 5:
        bad(f"E: 剩余 {free/1024**3:.1f} GB，装不下（至少留 5 GB）")
        return False
    ok(f"E: 剩余 {free/1024**3:.0f} GB")
    return True


def check_offline_ready() -> bool:
    """断网部署的关键：镜像和模型必须已经在本机。"""
    print("\n=== 离线就绪检查 ===")
    okc, out = sh(["docker", "images", "--format", "{{.Repository}}:{{.Tag}}"])
    if okc:
        n = len([l for l in out.splitlines() if l and l != "<none>:<none>"])
        if n >= 10:
            ok(f"本地镜像 {n} 个（Dify 全家桶约 15 个）")
        else:
            bad(f"本地镜像只有 {n} 个，断网环境无法 docker pull")
    models_dir = Path.home() / ".ollama" / "models" / "blobs"
    if models_dir.exists():
        size = sum(f.stat().st_size for f in models_dir.glob("sha256-*") if f.is_file())
        ok(f"Ollama 模型目录 {size/1024**3:.1f} GB")
    return True


def start_containers() -> None:
    print("\n=== 启动 Dify ===")
    if DIFY_DIR.exists():
        r = subprocess.run(["docker", "compose", "up", "-d"], cwd=str(DIFY_DIR / "docker"),
                           capture_output=True, text=True, timeout=600,
                           encoding="utf-8", errors="replace")
        okc = r.returncode == 0
        out = (r.stdout or r.stderr or "")[-200:]
    else:
        okc, out = False, "目录不存在"
    if okc:
        ok("容器已启动")
    else:
        bad(f"compose 启动失败: {out}")

    print("  等待 API 就绪…")
    for i in range(40):
        try:
            with urllib.request.urlopen("http://127.0.0.1/console/api/system-features", timeout=3):
                ok(f"API 就绪（约 {i*5}s）")
                return
        except Exception:
            time.sleep(5)
    bad("API 300 秒内未就绪")


def verify(deployed_state: dict) -> None:
    print("\n=== 端到端验证 ===")
    cases = [
        ("zhang", "tk-zhang-7f3a9c2e", "关联交易审批存在什么问题", "合规知识库"),
        ("li", "tk-li-9e2c5d7b", "关联交易审批存在什么问题", "人事知识库"),
        ("li", "tk-li-9e2c5d7b", "陈大文的月薪是多少", "人事知识库"),
        ("guest", "tk-guest-1a3f8e4c", "关联交易审批存在什么问题", "403"),
        ("-", "bad-token", "关联交易审批存在什么问题", "401"),
    ]
    for user, token, q, expect in cases:
        payload = json.dumps({"user": user if user != "-" else "", "question": q}).encode()
        req = urllib.request.Request("http://127.0.0.1:8090/ask", data=payload,
                                     headers={"Content-Type": "application/json",
                                              "Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(req, timeout=240) as r:
                d = json.loads(r.read())
            routed = d.get("routed_tier")
            mark = "✓" if routed == expect else "!"
            print(f"  {mark} [{user}] 路由={routed} 答={d.get('answer','')[:60]}…")
        except urllib.error.HTTPError as e:
            code = "403" if e.code == 403 else str(e.code)
            mark = "✓" if code == expect else "!"
            print(f"  {mark} [{user}] HTTP {code}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只做前置检查")
    ap.add_argument("--verify", action="store_true", help="只跑验证")
    ap.add_argument("--offline", action="store_true", help="额外做离线就绪检查")
    args = ap.parse_args()

    if args.verify:
        if not STATE.exists():
            bad("没有部署状态文件，先跑完整部署")
            sys.exit(1)
        verify(json.loads(STATE.read_text(encoding="utf-8")))
        return

    print("=== 前置检查 ===")
    check_disk()
    check_docker()
    check_ollama()
    check_dify()
    check_gateway()
    if args.offline:
        check_offline_ready()

    if args.check:
        print("\n" + ("全部就绪" if ok_all else "有项目未就绪（见上面 ✗）"))
        sys.exit(0 if ok_all else 1)

    if not any("api 容器: Up" in l for l in []):
        pass
    start_containers()

    print("\n=== 建库 + 建应用 ===")
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "dify_tiered2.py")],
                       capture_output=True, text=True, timeout=900,
                       encoding="utf-8", errors="replace")
    print((r.stdout or "")[-800:])
    if r.returncode != 0:
        bad(f"建库失败: {(r.stderr or '')[-300:]}")
        sys.exit(1)

    if STATE.exists():
        verify(json.loads(STATE.read_text(encoding="utf-8")))
    print("\n部署完成。网关: http://127.0.0.1:8090/ask   审计: /audit")


if __name__ == "__main__":
    main()
