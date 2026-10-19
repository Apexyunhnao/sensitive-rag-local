"""本地 Web 界面：把已有的能力露出来，变成能点、能拖文件的应用。

设计原则：
1. **不依赖任何 CDN**。这是内网系统，外网资源加载不到，页面会瞎。
   所以 CSS/JS 全部内嵌，图标用内联 SVG。
2. **只露出真做过的能力**。界面上有几个按钮，背后就得有几个真功能。
   做假卡片（摆个「研究分析专家」其实什么都没接）演示时必穿帮。
3. **身份可切换**。同一问题不同人问，答案不同 —— 这是这个项目最值得演示的点，
   藏在命令行里没人看得见。

启动：
    .venv/Scripts/python.exe scripts/app.py
打开：
    http://127.0.0.1:8092
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import uvicorn
from fastapi import FastAPI, File, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse

from contract_diff import compare_text
from pdf_to_md import extract as pdf_extract

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
GATEWAY = "http://127.0.0.1:8090"
OLLAMA = "http://127.0.0.1:11434"
PORT = 8092

# 演示用的身份：真实系统从 SSO / AD 来，令牌由网关校验。
# 这里每个身份带网关要求的 token，前端只能选、不能改 groups。
IDENTITIES = {
    "huang": {"name": "黄文浩", "dept": "法务合规部", "groups": ["compliance"],
              "token": "token-huang-compliance"},
    "li": {"name": "李四", "dept": "人事部", "groups": ["hr"],
           "token": "tk-li-9e2c5d7b"},
    "guest": {"name": "访客", "dept": "无", "groups": [],
              "token": "tk-guest-1a3f8e4c"},
}

app = FastAPI(title="本地敏感数据 AI 助手")

# GuaDa 前端（:5173）要跨端口调这个服务，必须放行 CORS。
# 只允许本机来源：这是内网单机系统，不开公网。
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:5173", "http://localhost:5173",
        "http://127.0.0.1:3000", "http://localhost:3000",
        "http://127.0.0.1:8092", "http://localhost:8092",
    ],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _probe(url: str, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse((WEB / "index.html").read_text(encoding="utf-8"))


@app.get("/api/status")
def status() -> dict:
    """服务状态。页面顶上的「本地运行」灯看的就是这个。"""
    return {
        "ollama": _probe(f"{OLLAMA}/api/tags"),
        "gateway": _probe(f"{GATEWAY}/audit"),
        "identities": {k: {"name": v["name"], "dept": v["dept"]} for k, v in IDENTITIES.items()},
    }


# ── 项目总览：扫本地项目目录，给出「每个项目现在什么状态」 ──────────────
PROJECTS_ROOT = Path(os.getenv("PROJECTS_ROOT", r"E:\Projects"))


def _git_run(cwd: Path, *args: str, timeout: int = 6) -> tuple[bool, str]:
    """跑一条 git 命令，返回 (是否成功, stdout)。

    不再把非 0 返回码静默吞成空字符串 —— 否则「仓库损坏」会和「字段本来就空」
    长得一模一样，页面上看不出任何异常。
    """
    try:
        out = subprocess.run(
            ["git", *args], cwd=str(cwd), capture_output=True, text=True,
            timeout=timeout, encoding="utf-8", errors="replace",
        )
        return out.returncode == 0, out.stdout.strip()
    except Exception:
        return False, ""


def _git(cwd: Path, *args: str, timeout: int = 6) -> str:
    return _git_run(cwd, *args, timeout=timeout)[1]


# —— 项目总览：哪些目录算项目 ——
# 第一层：硬编码排除明显噪音（临时/试验/备份/数据目录）
_EXCLUDE_KEYWORDS = ("backup", "tmp")
_EXCLUDE_SUFFIX = ("-data", "_data")

# 第二层：非 git 目录靠信号判断是不是真项目
_PROJECT_MARKERS = (
    "package.json", "pyproject.toml", "requirements.txt", "Cargo.toml",
    "go.mod", "pom.xml", "build.gradle", "README.md", "README.en.md",
)
_SOURCE_EXTS = {".py", ".ts", ".tsx", ".js", ".jsx", ".vue", ".java", ".go", ".rs", ".kt"}
_SKIP_DIRS = {
    ".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build",
    ".next", "target", ".idea", ".vscode", "file_stores",
}


def _is_noise_dir(name: str) -> tuple[bool, str]:
    low = name.lower()
    if name.startswith("_"):
        return True, "下划线开头（临时/试验目录）"
    if any(k in low for k in _EXCLUDE_KEYWORDS):
        return True, "名字含备份/临时关键词"
    if low.endswith(_EXCLUDE_SUFFIX):
        return True, "数据/备份目录"
    return False, ""


def _count_sources(d: Path, limit: int = 50, max_depth: int = 3) -> int:
    """数二级～三级以内的源码文件（剪掉 node_modules 等，带上限，不做全盘遍历）。"""
    n = 0
    base = len(str(d))
    try:
        for root, dirs, files in os.walk(d):
            rel = str(root)[base:]
            if rel.count(os.sep) >= max_depth:
                dirs[:] = []
                continue
            dirs[:] = [x for x in dirs if x not in _SKIP_DIRS and not x.startswith(".")]
            for f in files:
                if Path(f).suffix.lower() in _SOURCE_EXTS:
                    n += 1
                    if n >= limit:
                        return n
    except Exception:  # noqa: BLE001
        pass
    return n


def _project_signals(d: Path) -> tuple[bool, str]:
    for m in _PROJECT_MARKERS:
        if (d / m).exists():
            return True, m
    cnt = _count_sources(d)
    if cnt >= 5:
        return True, f"{cnt}+ 个源码文件"
    return False, ""



def _scan_one(d: Path) -> dict:
    """一个目录 → 一条项目记录。全部来自文件系统和 git，不调模型。"""
    noise, noise_reason = _is_noise_dir(d.name)
    info: dict = {
        "name": d.name,
        "path": str(d),
        "is_git": False,
        "broken": False,          # .git 在但 HEAD 无效（损坏/空仓库）
        "branch": "",
        "upstream": "",
        "last_commit": "",
        "last_commit_time": "",
        "dirty": 0,
        "ahead": None,            # None = 没有上游分支 / 不知道推没推，不要当成 0
        "mtime": 0,
        "readme": "",
        "size_mb": 0,
        "lang": "",
        "is_project": True,
        "project_signal": "",
        "excluded": noise,
        "exclude_reason": noise_reason,
    }
    try:
        info["mtime"] = int(d.stat().st_mtime)
    except Exception:
        pass

    # 判是不是 git 仓库：跑命令看 exit code（`.git` 存在性判断不了 worktree / 损坏仓库）
    info["is_git"] = _git_run(d, "rev-parse", "--is-inside-work-tree")[0]

    if info["is_git"]:
        head_ok, _ = _git_run(d, "rev-parse", "--verify", "HEAD")
        if not head_ok:
            # .git 在、但 HEAD 指向不存在的提交（空仓库或损坏）—— 明确标出来
            info["broken"] = True
        else:
            info["branch"] = _git(d, "rev-parse", "--abbrev-ref", "HEAD")
            info["last_commit"] = _git(d, "log", "-1", "--pretty=%s")
            info["last_commit_time"] = _git(d, "log", "-1", "--pretty=%cI")
            st = _git(d, "status", "--porcelain")
            info["dirty"] = len([x for x in st.splitlines() if x.strip()])
            up_ok, up = _git_run(d, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
            if up_ok and up:
                info["upstream"] = up
                cnt_ok, cnt = _git_run(d, "rev-list", "--count", "@{u}..HEAD")
                info["ahead"] = int(cnt) if cnt_ok and cnt.isdigit() else None

    if info["is_git"]:
        info["is_project"] = True
        info["project_signal"] = "git 仓库"
    else:
        sig, why = _project_signals(d)
        info["is_project"] = sig
        info["project_signal"] = why

    for cand in ("README.md", "README.en.md", "readme.md"):
        p = d / cand
        if p.exists():
            try:
                lines = [
                    ln.strip() for ln in p.read_text(encoding="utf-8", errors="ignore").splitlines()
                    if ln.strip() and not ln.startswith("#") and not ln.startswith(">") and not ln.startswith("!")
                ]
                info["readme"] = " ".join(lines[:2])[:160]
                info["readme_file"] = cand
            except Exception:
                pass
            break

    # 语言线索（只看顶层，避免遍历大目录）
    try:
        exts: dict[str, int] = {}
        for child in list(d.iterdir())[:200]:
            if child.is_file():
                exts[child.suffix.lower()] = exts.get(child.suffix.lower(), 0) + 1
        for sub in ("src", "app", "backend", "frontend"):
            sp = d / sub
            if sp.is_dir():
                for f in list(sp.rglob("*"))[:120]:
                    if f.is_file():
                        exts[f.suffix.lower()] = exts.get(f.suffix.lower(), 0) + 1
        mapping = {
            ".py": "Python", ".ts": "TypeScript", ".js": "JavaScript", ".vue": "Vue",
            ".java": "Java", ".go": "Go", ".rs": "Rust", ".kt": "Kotlin",
        }
        best = max(
            ((mapping[k], v) for k, v in exts.items() if k in mapping),
            key=lambda x: x[1], default=("", 0),
        )
        info["lang"] = best[0]
    except Exception:
        pass

    return info


def _week_ago() -> str:
    import datetime as _dt
    return (_dt.datetime.now() - _dt.timedelta(days=7)).strftime("%Y-%m-%d")


# —— Agent 能力演示：本地自愈 agent ——
# 直接跑 self-healing-agent 的 demo_cycle.py（探针→故障注入→修复→验证→清理），
# 把它的 stdout 解析成分步时间线返回给界面。不伪造：拿不到真实输出就报错。
SELF_HEAL_DIR = Path(os.getenv("SELF_HEAL_DIR", r"E:\Projects\self-healing-agent"))


def _parse_heal_output(text: str) -> list[dict]:
    """把 demo_cycle.py 的输出切成 [{'title': '1. …', 'lines': [...]}, …]。"""
    steps: list[dict] = []
    cur: dict | None = None
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        if line.startswith("===") and line.endswith("==="):
            title = line.strip("= ").strip()
            cur = {"title": title, "lines": []}
            steps.append(cur)
            continue
        if cur is None:
            cur = {"title": "输出", "lines": []}
            steps.append(cur)
        cur["lines"].append(line)
    return steps


@app.post("/api/agent/heal")
def agent_heal() -> JSONResponse:
    """真跑一次自愈闭环演示（约 10–30 秒）。"""
    script = SELF_HEAL_DIR / "demo_cycle.py"
    if not script.exists():
        return JSONResponse({"ok": False, "error": f"找不到 {script}"}, status_code=404)
    t0 = time.time()
    try:
        out = subprocess.run(
            [sys.executable, str(script)],
            cwd=str(SELF_HEAL_DIR), capture_output=True, text=True,
            timeout=180, encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired:
        return JSONResponse({"ok": False, "error": "自愈演示超时（180 秒）"}, status_code=504)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": f"启动失败：{e}"}, status_code=500)

    stdout = out.stdout or ""
    success = "集成闭环: 成功" in stdout
    return JSONResponse({
        "ok": out.returncode == 0 and success,
        "success": success,
        "returncode": out.returncode,
        "elapsed_ms": round((time.time() - t0) * 1000),
        "steps": _parse_heal_output(stdout),
        "stderr": (out.stderr or "")[-1500:],
        "raw": stdout[-4000:],
    })



@app.get("/api/projects")
def projects() -> dict:
    """列出项目目录下的所有项目及其真实状态（git 分支/最后提交/未提交数）。

    过滤分两层（2026-10-19 加，之前是「E:\\Projects 下每个目录都算项目」）：
    1. 硬编码排除：下划线开头（临时/试验）、名字含 backup / tmp、以 -data / _data 结尾
    2. 非 git 目录看信号：有 package.json / requirements.txt / README 等标记，或有 ≥5 个源码文件

    被排掉的目录不丢，放在 `excluded` 里（带原因），页面可展开查看 ——
    避免出现「我的目录怎么不见了」这种黑箱感。
    """
    t0 = time.time()
    if not PROJECTS_ROOT.is_dir():
        return {"ok": False, "error": f"目录不存在：{PROJECTS_ROOT}", "projects": []}

    try:
        dirs = [d for d in PROJECTS_ROOT.iterdir() if d.is_dir() and not d.name.startswith(".")]
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e), "projects": []}

    with ThreadPoolExecutor(max_workers=8) as ex:
        items = list(ex.map(_scan_one, dirs))

    kept = [i for i in items if not i["excluded"] and i["is_project"]]
    dropped = [
        {
            "name": i["name"],
            "reason": i["exclude_reason"]
            or "没有项目特征（无 package.json / README，源码文件 < 5）",
        }
        for i in items
        if i["excluded"] or not i["is_project"]
    ]
    kept.sort(key=lambda x: x.get("last_commit_time") or "", reverse=True)

    week = _week_ago()

    def _count(fn) -> int:
        return sum(1 for i in kept if fn(i))

    return {
        "ok": True,
        "root": str(PROJECTS_ROOT),
        "count": len(kept),
        "git_count": _count(lambda i: i["is_git"]),
        "untracked_count": _count(lambda i: not i["is_git"]),
        "dirty_count": _count(lambda i: (i["dirty"] or 0) > 0),
        "unpushed_count": _count(lambda i: (i["ahead"] or 0) > 0),
        "broken_count": _count(lambda i: i["broken"]),
        "recent_count": _count(lambda i: i.get("last_commit_time", "")[:10] >= week),
        "scanned_total": len(items),
        "excluded_count": len(dropped),
        "elapsed_ms": round((time.time() - t0) * 1000),
        "projects": kept,
        "excluded": dropped,
    }



def _diff_files(pa: Path, pb: Path, name_a: str, name_b: str) -> JSONResponse:
    """比对两个本地 PDF。上传接口和「按路径比对」共用这一段。"""
    t0 = time.time()
    try:
        text_a = pdf_extract(pa)
        text_b = pdf_extract(pb)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": f"PDF 解析失败：{e}"}, status_code=400)

    if len(text_a.strip()) < 50 or len(text_b.strip()) < 50:
        return JSONResponse(
            {"error": "解析出来的文字太少（可能是扫描件或图片型 PDF）。"
                      "本工具只支持文字型 PDF。"}, status_code=400)

    result = compare_text(text_a, text_b)
    result["elapsed_ms"] = int((time.time() - t0) * 1000)
    result["files"] = {"old": name_a, "new": name_b}
    return JSONResponse(result)


@app.post("/api/diff")
async def api_diff(old_file: UploadFile = File(...), new_file: UploadFile = File(...)) -> JSONResponse:
    """两份 PDF → 条款级改动清单。纯本地，0 模型。"""
    tmp = Path(tempfile.mkdtemp(prefix="diff_"))
    try:
        pa, pb = tmp / "old.pdf", tmp / "new.pdf"
        pa.write_bytes(await old_file.read())
        pb.write_bytes(await new_file.read())
        return _diff_files(pa, pb, old_file.filename or "old.pdf", new_file.filename or "new.pdf")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@app.post("/api/diff_by_path")
async def api_diff_by_path(payload: dict) -> JSONResponse:
    """给 GuaDa 插件用的：直接按本地路径比对（同一个机器，不用传文件流）。

    payload: {"old": "...pdf", "new": "...pdf"}
    两个路径都必须在项目目录或 GuaDa 上传目录下 —— 不允许任意读盘。
    """
    raw_a, raw_b = payload.get("old"), payload.get("new")
    if not raw_a or not raw_b:
        return JSONResponse({"error": "需要 old 和 new 两个文件路径"}, status_code=400)

    allowed_roots = [ROOT.resolve(), (ROOT / "data").resolve()]
    guada_uploads = Path(payload.get("uploads_root") or "").resolve() if payload.get("uploads_root") else None
    if guada_uploads:
        allowed_roots.append(guada_uploads)

    def _check(p: str) -> Path | None:
        path = Path(p).resolve()
        if path.suffix.lower() != ".pdf" or not path.exists():
            return None
        if not any(str(path).startswith(str(r)) for r in allowed_roots):
            return None
        return path

    pa, pb = _check(raw_a), _check(raw_b)
    if not pa or not pb:
        return JSONResponse(
            {"error": "路径无效：文件不存在、不是 PDF，或不在允许的目录内"},
            status_code=400)
    return _diff_files(pa, pb, pa.name, pb.name)


@app.get("/api/audit")
def api_audit(limit: int = 100) -> dict:
    """审计日志代理：只返回行为记录（谁问了、路由到哪个库、是否被拦、耗时），不含问题原文。"""
    try:
        with urllib.request.urlopen(f"{GATEWAY}/audit", timeout=5) as r:
            data = json.loads(r.read().decode())
        records = data.get("records", [])[:limit]
        return {"ok": True, "count": len(records), "records": records}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"网关不可达：{e}", "records": []}


@app.get("/api/skills")
def api_skills() -> dict:
    """能力清单。GuaDa 插件启动时用它自检后端是否在线。"""
    return {
        "skills": [
            {"id": "contract_diff", "name": "合同版本比对",
             "desc": "两版合同 PDF → 条款级改动清单。0 模型，0.6 秒内出结果。"},
            {"id": "kb_ask", "name": "知识库问答",
             "desc": "按身份查对应密级的知识库（Dify + 本地 7B）。"},
            {"id": "ocr_extract", "name": "图片识别",
             "desc": "本地 OCR 提字段，身份证走 GB11643 校验位。"},
        ],
        "status": {"ollama": _probe(f"{OLLAMA}/api/tags"), "gateway": _probe(f"{GATEWAY}/audit")},
    }


@app.post("/api/ask")
async def api_ask(payload: dict) -> JSONResponse:
    """问答：转给权限网关（:8090），由网关按身份选密级库。"""
    q = (payload.get("question") or "").strip()
    who = payload.get("identity") or "huang"
    if not q:
        return JSONResponse({"error": "问题不能为空"}, status_code=400)
    ident = IDENTITIES.get(who)
    if not ident:
        return JSONResponse({"error": "未知身份"}, status_code=400)

    body = json.dumps({"user": who, "question": q}, ensure_ascii=False).encode()
    req = urllib.request.Request(
        f"{GATEWAY}/ask", data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {ident['token']}"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=200) as r:
            data = json.loads(r.read().decode())
        return JSONResponse({"ok": True, "answer": data.get("answer", ""),
                             "tier": data.get("tier", ""), "sources": data.get("sources", []),
                             "latency_ms": int((time.time() - t0) * 1000),
                             "who": ident["name"]})
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:200]
        hint = {401: "令牌无效", 403: "该身份没有可访问的知识库"}.get(e.code, f"网关返回 {e.code}")
        return JSONResponse({"ok": False, "error": f"{hint}（{detail}）"},
                            status_code=200)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": f"网关没起来或超时：{e}"}, status_code=200)


def main() -> int:
    # 这个服务的两个用途：
    #   1. 独立页面（web/index.html 存在时）—— 单机演示用
    #   2. 纯 API 后端（被 GuaDa 工作台调用）—— 本项目当前的主用法
    # 所以缺前端文件不该直接退出，只提示。
    if not (WEB / "index.html").exists():
        print(f"提示：{WEB / 'index.html'} 不存在，只提供 API（供 GuaDa 工作台调用）。")
    else:
        print(f"界面：http://127.0.0.1:{PORT}")
    print(f"API：http://127.0.0.1:{PORT}/api/status")
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
