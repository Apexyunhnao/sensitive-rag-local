"""给应用建 API key 并跑一次真实问答，验证 Dify → 本地 Ollama 的完整链路。"""
from __future__ import annotations

import json
import sys
import time
import urllib.request

from playwright.sync_api import sync_playwright

PROFILE = "C:/Users/31619/AppData/Local/hermes/cache/dify-browser"
BASE = "http://127.0.0.1"
APP_ID = "e7349827-f654-4f60-8473-1068a786ada6"


def post(url: str, payload: dict, headers: dict, timeout: int = 180) -> tuple[int, str]:
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:400]


def main() -> None:
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            PROFILE, channel="msedge", headless=True,
            viewport={"width": 1440, "height": 900})
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(f"{BASE}/apps", wait_until="domcontentloaded")
        page.wait_for_timeout(1500)

        # 1) 取/建 API key
        res = page.evaluate("""async (appId) => {
            const m = document.cookie.match(/csrf_token=([^;]+)/);
            const csrf = m ? decodeURIComponent(m[1]) : '';
            const r = await fetch(`/console/api/apps/${appId}/api-keys`, {
                headers: {'X-CSRF-Token': csrf}, credentials: 'include'});
            return {status: r.status, body: await r.text()};
        }""", APP_ID)
        keys = json.loads(res["body"]).get("data", []) if res["status"] == 200 else []
        if keys:
            api_key = keys[0]["token"]
            print("复用已有 key:", api_key[:12] + "...")
        else:
            res = page.evaluate("""async (appId) => {
                const m = document.cookie.match(/csrf_token=([^;]+)/);
                const csrf = m ? decodeURIComponent(m[1]) : '';
                const r = await fetch(`/console/api/apps/${appId}/api-keys`, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf},
                    credentials: 'include',
                    body: JSON.stringify({})
                });
                return {status: r.status, body: await r.text()};
            }""", APP_ID)
            if res["status"] not in (200, 201):
                print("建 key 失败:", res["status"], res["body"][:300])
                ctx.close()
                sys.exit(1)
            api_key = json.loads(res["body"])["token"]
            print("新建 key:", api_key[:12] + "...")

        ctx.close()

    # 2) 调应用 API 提问
    print("\n=== 提问：关联交易审批存在什么问题 ===")
    t0 = time.time()
    st, body = post(f"{BASE}/v1/chat-messages", {
        "inputs": {},
        "query": "关联交易审批存在什么问题",
        "response_mode": "blocking",
        "conversation_id": "",
        "user": "tester",
    }, {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})
    elapsed = time.time() - t0

    if st != 200:
        print("失败:", st, body[:400])
        sys.exit(1)
    d = json.loads(body)
    print(f"回答: {d.get('answer', '')[:400]}")
    print(f"\n耗时: {elapsed:.1f}s")
    print(f"用量: {json.dumps(d.get('metadata', {}).get('usage', {}))}")
    res_list = d.get("metadata", {}).get("retriever_resources", [])
    print(f"检索命中 {len(res_list)} 段:")
    for r in res_list[:5]:
        print(f"  - {r.get('document_name')} (score={r.get('score')})")


if __name__ == "__main__":
    main()
