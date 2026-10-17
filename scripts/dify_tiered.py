"""按密级拆分知识库 + 建对应应用（权限落地的基础）。

Dify 的 chat API 不能动态指定知识库，所以权限隔离只能靠：
一个密级一个知识库，一个知识库绑定一个应用，网关按用户身份路由。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import requests

BASE = "http://127.0.0.1"
AUTH = json.loads(Path("E:/Projects/sensitive-rag-demo/.dify-auth.json").read_text(encoding="utf-8"))
H = {
    "Authorization": f"Bearer {AUTH['access_token']}",
    "X-CSRF-Token": AUTH["csrf_token"],
}
# Dify 的 CSRF 校验依赖 cookie，必须用 Session 带上完整 cookie
S = requests.Session()
for _k, _v in AUTH.get("cookies", {}).items():
    S.cookies.set(_k, _v, domain="127.0.0.1", path="/")
DATA = Path("E:/Projects/sensitive-rag-demo/data")

# 密级 → 文档 → 部门
LEVELS = {
    "合规知识库": {
        "docs": ["hkex_announcement_2026_041.md", "internal_audit_2026_q3.md"],
        "groups": ["compliance", "board"],
    },
    "人事知识库": {
        "docs": ["hr_roster.md"],
        "groups": ["hr"],
    },
}


def create_dataset(name: str) -> str | None:
    payload = {
        "name": name,
        "description": f"{name}（按密级隔离）",
        "indexing_technique": "high_quality",
        "permission": "only_me",
        "provider": "vendor",
        "embedding_model": "bge-m3",
        "embedding_model_provider": "langgenius/ollama/ollama",
        "retrieval_model": {
            "search_method": "hybrid_search",
            "reranking_enable": False,
            "reranking_model": {"reranking_provider_name": "", "reranking_model_name": ""},
            "top_k": 5,
            "score_threshold_enabled": False,
            "score_threshold": 0.5,
            "weights": {
                "weight_type": "customized",
                "vector_setting": {"vector_weight": 0.5, "embedding_provider_name": "", "embedding_model_name": ""},
                "keyword_setting": {"keyword_weight": 0.5},
            },
        },
    }
    r = S.post(f"{BASE}/console/api/datasets", headers=H, json=payload, timeout=60)
    if r.status_code not in (200, 201):
        print(f"  ✗ 建库失败 {r.status_code}: {r.text[:200]}")
        return None
    ds = r.json()
    print(f"  ✓ 知识库已建: {ds['name']} id={ds['id']}")
    return ds["id"]


def upload(ds_id: str, path: Path) -> bool:
    process = {
        "indexing_technique": "high_quality",
        "process_rule": {"mode": "automatic"},
    }
    files = {"file": (path.name, path.read_bytes(), "text/markdown")}
    r = S.post(
        f"{BASE}/console/api/datasets/{ds_id}/document/create-by-file",
        headers=H,
        data={"data": json.dumps(process)},
        files=files,
        timeout=180,
    )
    if r.status_code not in (200, 201):
        print(f"  ✗ 上传失败 {path.name} {r.status_code}: {r.text[:200]}")
        return False
    d = r.json()
    doc = d.get("document", {})
    print(f"  ✓ {path.name} → doc_id={doc.get('id')} status={doc.get('indexing_status')}")
    return True


def wait_indexed(ds_id: str, timeout: int = 300) -> bool:
    """等所有文档 indexing_status=completed。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        r = S.get(f"{BASE}/console/api/datasets/{ds_id}/documents?page=1&limit=20", headers=H, timeout=30)
        if r.status_code != 200:
            return False
        docs = r.json().get("data", [])
        states = [d.get("indexing_status") for d in docs]
        if docs and all(s == "completed" for s in states):
            return True
        if any(s in ("error", "paused") for s in states):
            print("  ! 有文档索引失败:", states)
            return False
        time.sleep(5)
    print("  ! 索引超时")
    return False


def create_app(name: str, ds_id: str) -> tuple[str, str] | None:
    r = S.post(f"{BASE}/console/api/apps", headers=H,
                      json={"name": name, "mode": "chat", "icon": "🤖", "icon_background": "#E0F2FE"},
                      timeout=30)
    if r.status_code not in (200, 201):
        print(f"  ✗ 建应用失败 {r.status_code}: {r.text[:200]}")
        return None
    app = r.json()
    app_id = app["id"]
    print(f"  ✓ 应用壳已建: {name} id={app_id}")

    # 配模型 + 知识库（创建时传 model_config 会被忽略，必须另外配）
    mc = {
        "model": {
            "provider": "langgenius/ollama/ollama",
            "name": "qwen2.5:7b",
            "mode": "chat",
            "completion_params": {"temperature": 0.1, "max_tokens": 1024},
        },
        "prompt_template": {
            "text": ("你是企业知识助手。根据资料回答问题，只用资料中已有的信息，"
                     "资料没有就说\"未提及\"。回答简洁，末尾标注来源文档名。\n\n资料：\n{{#context#}}"),
            "variables": [],
        },
        "dataset_configs": {"retrieval_model": "multiple", "datasets": {"datasets": [{"dataset_id": ds_id}]}},
        "opening_statement": "",
        "suggested_questions": [],
        "retriever_resource": {"enabled": True},
        "sensitive_word_avoidance": {"enabled": False},
        "speech_to_text": {"enabled": False},
        "text_to_speech": {"enabled": False},
        "more_like_this": {"enabled": False},
        "agent_mode": {"enabled": False},
    }
    r = S.post(f"{BASE}/console/api/apps/{app_id}/model-config", headers=H, json=mc, timeout=60)
    print(f"    model-config: {r.status_code}")
    if r.status_code not in (200, 201):
        print("    ", r.text[:200])

    # 发布
    r = S.post(f"{BASE}/console/api/apps/{app_id}/workflows/publish", headers=H, json={}, timeout=60)
    print(f"    publish(workflow): {r.status_code}")

    # 建 API key
    r = S.post(f"{BASE}/console/api/apps/{app_id}/api-keys", headers=H, json={}, timeout=30)
    if r.status_code not in (200, 201):
        print(f"    ✗ 建 key 失败: {r.text[:150]}")
        return None
    key = r.json()["token"]
    print(f"    ✓ api key: {key[:14]}...")
    return app_id, key


def main() -> None:
    result = {}
    for ds_name, cfg in LEVELS.items():
        print(f"\n=== {ds_name} ===")
        ds_id = create_dataset(ds_name)
        if not ds_id:
            continue
        for doc in cfg["docs"]:
            p = DATA / doc
            if p.exists():
                upload(ds_id, p)
            else:
                print(f"  ! 缺文件 {doc}")
        if not wait_indexed(ds_id):
            print("  ! 索引未完成，跳过建应用")
            continue
        print("  ✓ 索引全部完成")
        app = create_app(ds_name.replace("知识库", "助手"), ds_id)
        if app:
            result[ds_name] = {"dataset_id": ds_id, "app_id": app[0], "api_key": app[1],
                               "groups": cfg["groups"]}

    out = Path("E:/Projects/sensitive-rag-demo/.dify-tiered.json")
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果已存: {out}")
    for k, v in result.items():
        print(f"  {k}: app={v['app_id'][:8]}... groups={v['groups']}")


if __name__ == "__main__":
    main()
