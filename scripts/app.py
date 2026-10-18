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
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request
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
    "zhang": {"name": "张三", "dept": "人事部", "groups": ["hr"],
              "token": "token-zhang-hr"},
    "guest": {"name": "访客", "dept": "无", "groups": [],
              "token": "token-guest-none"},
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
