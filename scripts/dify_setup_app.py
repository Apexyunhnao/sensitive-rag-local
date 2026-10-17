"""用已登录的 Dify 会话直接调 Console API：列出知识库，创建聊天助手应用并关联知识库。

省去在界面上点十几下的麻烦。登录态从持久化 profile 读取。
只用标准库（urllib），免装依赖。
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

from playwright.sync_api import sync_playwright

PROFILE = "C:/Users/31619/AppData/Local/hermes/cache/dify-browser"
BASE = "http://127.0.0.1"


def api(path: str, token: str, csrf: str, method: str = "GET", body: dict | None = None):
    url = f"{BASE}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "X-CSRF-Token": csrf,
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:400]


def get_token() -> tuple[str, str]:
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            PROFILE, channel="msedge", headless=True,
            viewport={"width": 1440, "height": 900},
        )
        ctx.new_page().goto(f"{BASE}/apps", wait_until="domcontentloaded")
        jar = {c["name"]: c["value"] for c in ctx.cookies()}
        ctx.close()
    tok, csrf = jar.get("access_token"), jar.get("csrf_token", "")
    if not tok:
        sys.exit("拿不到 access_token，先跑 dify_login.py")
    return tok, csrf


def main() -> None:
    token, csrf = get_token()
    print("token 前缀:", token[:12], "...")

    st, data = api("/console/api/datasets?page=1&limit=20", token, csrf)
    print("datasets:", st)
    if st != 200:
        sys.exit(f"列知识库失败: {data}")
    for ds in data.get("data", []):
        print(f"  {ds['name']}  id={ds['id']}")

    target = next((d for d in data.get("data", []) if d["name"].startswith("hkex")), None)
    if not target:
        sys.exit("找不到 hkex 开头的知识库")
    ds_id = target["id"]
    print(f"\n目标知识库: {target['name']} ({ds_id})")

    payload = {
        "name": "港股公告审阅助手",
        "mode": "chat",
        "icon": "🤖",
        "icon_background": "#FFEAD5",
        "model_config": {
            "model": {
                "provider": "langgenius/ollama/ollama",
                "name": "qwen2.5:7b",
                "mode": "chat",
                "completion_params": {"temperature": 0.1, "max_tokens": 1024},
            },
            "prompt_template": {
                "text": (
                    "你是一个港股公告审阅助手。根据以下资料回答问题，"
                    "只用资料中已有的信息，资料里没有就说\"未提及\"。"
                    "回答简洁，末尾标注来源文档名。\n\n资料：\n{{#context#}}"
                ),
                "variables": [],
            },
            "dataset_configs": {
                "retrieval_model": "multiple",
                "datasets": {"datasets": [{"dataset_id": ds_id}]},
            },
            "opening_statement": "",
            "suggested_questions": [],
            "retriever_resource": {"enabled": True},
            "sensitive_word_avoidance": {"enabled": False},
            "speech_to_text": {"enabled": False},
            "text_to_speech": {"enabled": False},
            "more_like_this": {"enabled": False},
            "agent_mode": {"enabled": False},
        },
    }
    st, app = api("/console/api/apps", token, csrf, method="POST", body=payload)
    print("create app:", st)
    if st not in (200, 201):
        print(app)
        sys.exit("创建失败")
    print(f"✓ 应用已创建: {app.get('name')}  id={app.get('id')}  mode={app.get('mode')}")
    print(json.dumps({"app_id": app.get("id"), "dataset_id": ds_id}, ensure_ascii=False))


if __name__ == "__main__":
    main()
