"""在 Dify 页面上下文里调 API（自动携带 cookie / CSRF），创建聊天助手应用。

比手动拼 header 可靠：浏览器上下文里的同源 fetch 天然带全部凭据。
"""
from __future__ import annotations

import json
import sys

from playwright.sync_api import sync_playwright

PROFILE = "C:/Users/31619/AppData/Local/hermes/cache/dify-browser"
BASE = "http://127.0.0.1"


def main() -> None:
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            PROFILE, channel="msedge", headless=True,
            viewport={"width": 1440, "height": 900},
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(f"{BASE}/apps", wait_until="domcontentloaded")
        page.wait_for_timeout(2000)

        # 1) 列知识库
        res = page.evaluate("""async () => {
            const m = document.cookie.match(/csrf_token=([^;]+)/);
            const csrf = m ? decodeURIComponent(m[1]) : '';
            const r = await fetch('/console/api/datasets?page=1&limit=20', {
                headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf },
                credentials: 'include'
            });
            return { status: r.status, body: await r.text() };
        }""")
        print("datasets:", res["status"])
        if res["status"] != 200:
            print(res["body"][:300])
            ctx.close()
            sys.exit(1)

        data = json.loads(res["body"])
        for ds in data.get("data", []):
            print(f"  {ds['name']}  id={ds['id']}")

        target = next((d for d in data.get("data", []) if "敏感" in d["name"]), None)
        if not target:
            ctx.close()
            sys.exit("找不到名为「敏感」的知识库")
        ds_id = target["id"]
        print(f"\n目标知识库: {target['name']} ({ds_id})")

        # 2) 创建聊天助手
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
        res = page.evaluate("""async (payload) => {
            const m = document.cookie.match(/csrf_token=([^;]+)/);
            const csrf = m ? decodeURIComponent(m[1]) : '';
            const r = await fetch('/console/api/apps', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf },
                credentials: 'include',
                body: JSON.stringify(payload)
            });
            return { status: r.status, body: await r.text() };
        }""", payload)
        print("create app:", res["status"])
        if res["status"] not in (200, 201):
            print(res["body"][:600])
            ctx.close()
            sys.exit(1)

        app = json.loads(res["body"])
        print(f"✓ 已创建: {app.get('name')}  id={app.get('id')}  mode={app.get('mode')}")
        print(json.dumps({"app_id": app.get("id"), "dataset_id": ds_id}, ensure_ascii=False))
        ctx.close()


if __name__ == "__main__":
    main()
