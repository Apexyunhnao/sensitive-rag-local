"""改名 + 诊断知识库关联是否真的生效。"""
from __future__ import annotations

import json

from playwright.sync_api import sync_playwright

PROFILE = "C:/Users/31619/AppData/Local/hermes/cache/dify-browser"
BASE = "http://127.0.0.1"
APP_ID = "e7349827-f654-4f60-8473-1068a786ada6"
NEW_NAME = "公告速读"


def main() -> None:
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            PROFILE, channel="msedge", headless=True,
            viewport={"width": 1440, "height": 900})
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(f"{BASE}/apps", wait_until="domcontentloaded")
        page.wait_for_timeout(1500)

        # 改名
        res = page.evaluate("""async ({appId, name}) => {
            const m = document.cookie.match(/csrf_token=([^;]+)/);
            const csrf = m ? decodeURIComponent(m[1]) : '';
            const r = await fetch(`/console/api/apps/${appId}`, {
                method: 'PATCH',
                headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf},
                credentials: 'include',
                body: JSON.stringify({name})
            });
            return {status: r.status, body: (await r.text()).slice(0, 300)};
        }""", {"appId": APP_ID, "name": NEW_NAME})
        print("改名:", res["status"], res["body"][:150])

        # 读回完整配置
        res = page.evaluate("""async (appId) => {
            const m = document.cookie.match(/csrf_token=([^;]+)/);
            const csrf = m ? decodeURIComponent(m[1]) : '';
            const r = await fetch(`/console/api/apps/${appId}`, {
                headers: {'X-CSRF-Token': csrf}, credentials: 'include'});
            return {status: r.status, body: await r.text()};
        }""", APP_ID)
        app = json.loads(res["body"])
        mc = app.get("model_config", {})
        print("\n当前应用名:", app.get("name"))
        print("模型:", mc.get("model", {}).get("provider"), "/", mc.get("model", {}).get("name"))
        print("dataset_configs:", json.dumps(mc.get("dataset_configs", {}), ensure_ascii=False)[:400])
        print("prompt 含 context 变量:", "{{#context#}}" in (mc.get("prompt_template", {}).get("text") or ""))

        # 知识库检索配置
        ds_id = "999671d0-b8f4-456f-afdf-9e0d28e7308b"
        res = page.evaluate("""async (dsId) => {
            const m = document.cookie.match(/csrf_token=([^;]+)/);
            const csrf = m ? decodeURIComponent(m[1]) : '';
            const r = await fetch(`/console/api/datasets/${dsId}/retrieve`, {
                method: 'POST',
                headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf},
                credentials: 'include',
                body: JSON.stringify({
                    query: "关联交易审批存在什么问题",
                    retrieval_model: {
                        search_method: "hybrid_search",
                        reranking_enable: false,
                        reranking_model: {reranking_provider_name: "", reranking_model_name: ""},
                        top_k: 5,
                        score_threshold_enabled: false,
                        score_threshold: 0.5,
                        weights: {weight_type: "customized", vector_setting: {vector_weight: 0.5, embedding_provider_name: "", embedding_model_name: ""}, keyword_setting: {keyword_weight: 0.5}}
                    }
                })
            });
            return {status: r.status, body: await r.text()};
        }""", ds_id)
        print("\n知识库直接检索:", res["status"])
        try:
            d = json.loads(res["body"])
            recs = d.get("records", [])
            print(f"命中 {len(recs)} 段")
            for r in recs[:3]:
                seg = r.get("segment", {})
                print(f"  score={r.get('score')} doc={seg.get('document', {}).get('name')} | {seg.get('content','')[:60]}")
        except Exception:
            print(res["body"][:300])

        ctx.close()


if __name__ == "__main__":
    main()
