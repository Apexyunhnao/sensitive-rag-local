"""补齐分库文档 + 建对应应用（用 service API，避开 console 的 CSRF/multipart 麻烦）。"""
from __future__ import annotations

import json
import time
from pathlib import Path

import requests

BASE = "http://127.0.0.1"
AUTH = json.loads(Path("E:/Projects/sensitive-rag-demo/.dify-auth.json").read_text(encoding="utf-8"))
DATA = Path("E:/Projects/sensitive-rag-demo/data")

S = requests.Session()
for k, v in AUTH.get("cookies", {}).items():
    S.cookies.set(k, v, domain="127.0.0.1", path="/")
CONSOLE = {"Authorization": f"Bearer {AUTH['access_token']}", "X-CSRF-Token": AUTH["csrf_token"]}

TIERS = {
    "合规知识库": {
        "dataset_id": "b388c6a6-9548-4707-8c93-6cbb496a93f5",
        "docs": ["hkex_announcement_2026_041.md"],
        "groups": ["compliance", "board"],
        "app_name": "合规助手",
    },
    "人事知识库": {
        "dataset_id": "0165b5d6-81fc-4bc2-847e-385b0d7b7f23",
        "docs": ["hr_roster.md"],
        "groups": ["hr"],
        "app_name": "人事助手",
    },
}


def get_dataset_key(ds_id: str) -> str:
    r = S.post(f"{BASE}/console/api/datasets/api-keys", headers=CONSOLE,
               json={"dataset_ids": [ds_id]}, timeout=30)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"建 dataset key 失败: {r.status_code} {r.text[:150]}")
    return r.json()["token"]


def upload(ds_id: str, key: str, path: Path) -> bool:
    files = {"file": (path.name, path.read_bytes(), "text/markdown")}
    data = {"data": json.dumps({"indexing_technique": "high_quality",
                                "process_rule": {"mode": "automatic"}})}
    r = requests.post(f"{BASE}/v1/datasets/{ds_id}/document/create-by-file",
                      headers={"Authorization": f"Bearer {key}"},
                      data=data, files=files, timeout=180)
    if r.status_code not in (200, 201):
        print(f"  ✗ {path.name}: {r.status_code} {r.text[:150]}")
        return False
    print(f"  ✓ {path.name}")
    return True


def docs_status(ds_id: str) -> list[tuple[str, str]]:
    r = S.get(f"{BASE}/console/api/datasets/{ds_id}/documents?page=1&limit=20",
              headers=CONSOLE, timeout=30)
    if r.status_code != 200:
        return []
    return [(d["name"], d.get("indexing_status")) for d in r.json().get("data", [])]


def wait_ready(ds_id: str, timeout: int = 420) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = docs_status(ds_id)
        if st and all(s == "completed" for _, s in st):
            return True
        if any(s == "error" for _, s in st):
            print("  ! 索引出错:", st)
            return False
        time.sleep(6)
    print("  ! 超时:", docs_status(ds_id))
    return False


def create_app(name: str, ds_id: str) -> tuple[str, str]:
    r = S.post(f"{BASE}/console/api/apps", headers=CONSOLE,
               json={"name": name, "mode": "chat", "icon": "🤖", "icon_background": "#E0F2FE"},
               timeout=30)
    r.raise_for_status()
    app_id = r.json()["id"]

    mc = {
        "model": {"provider": "langgenius/ollama/ollama", "name": "qwen2.5:7b", "mode": "chat",
                  "completion_params": {"temperature": 0.1, "max_tokens": 1024}},
        "prompt_template": {
            "text": ("你是企业知识助手。根据资料回答问题，只用资料中已有的信息，"
                     "资料没有就说\"未提及\"。回答简洁，末尾标注来源文档名。\n\n资料：\n{{#context#}}"),
            "variables": []},
        "dataset_configs": {"retrieval_model": "multiple",
                            "datasets": {"datasets": [{"dataset_id": ds_id}]}},
        "opening_statement": "", "suggested_questions": [],
        "retriever_resource": {"enabled": True},
        "sensitive_word_avoidance": {"enabled": False},
        "speech_to_text": {"enabled": False}, "text_to_speech": {"enabled": False},
        "more_like_this": {"enabled": False}, "agent_mode": {"enabled": False},
        "user_input_form": [],
    }
    r = S.post(f"{BASE}/console/api/apps/{app_id}/model-config", headers=CONSOLE, json=mc, timeout=60)
    ok_mc = r.status_code in (200, 201)
    r = S.post(f"{BASE}/console/api/apps/{app_id}/api-keys", headers=CONSOLE, json={}, timeout=30)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"建 app key 失败: {r.status_code} {r.text[:150]}")
    key = r.json()["token"]
    print(f"  ✓ 应用 {name} app_id={app_id[:8]}... model_config={ok_mc} key={key[:14]}...")
    return app_id, key


def main() -> None:
    out = {}
    for tier, cfg in TIERS.items():
        print(f"\n=== {tier} ===")
        key = get_dataset_key(cfg["dataset_id"])
        for d in cfg["docs"]:
            p = DATA / d
            if p.exists():
                upload(cfg["dataset_id"], key, p)

        print("  等待索引…")
        if not wait_ready(cfg["dataset_id"]):
            print("  ✗ 索引未完成，跳过建应用")
            continue
        print("  ✓ 索引完成:", docs_status(cfg["dataset_id"]))

        app_id, app_key = create_app(cfg["app_name"], cfg["dataset_id"])
        out[tier] = {"dataset_id": cfg["dataset_id"], "dataset_key": key,
                     "app_id": app_id, "app_key": app_key, "groups": cfg["groups"],
                     "app_name": cfg["app_name"]}

    f = Path("E:/Projects/sensitive-rag-demo/.dify-tiered.json")
    f.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已保存: {f}")
    for k, v in out.items():
        print(f"  {k} → {v['app_name']} ({v['app_id'][:8]}...) 组={v['groups']}")


if __name__ == "__main__":
    main()
