"""GuardRAG 最小闭环 —— 港股公告脱敏问答。

关键处理：
1. GuardRAG 默认 embedding 是 BAAI/bge-small-en-v1.5（英文），中文召回差，
   这里 monkey-patch 成 BAAI/bge-small-zh-v1.5。
2. 护栏不在 build_rag_chain 里，要用 guardrag.utils.safety 的
   check_input_safety / check_output_safety 显式调用。

用法：
    python scripts/demo.py            # 建库 + 提问
    python scripts/demo.py --rebuild  # 强制重建
"""
import argparse
import os
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "data" / "hkex_announcement_2026_041.md"
STORAGE = ROOT / "out" / "guardrag_storage"
STORAGE.mkdir(parents=True, exist_ok=True)

# 国内 HuggingFace 镜像（必须在导入 transformers 之前设）
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:7b")
SENSITIVITY = "Restricted"
# 脱敏默认关闭。目标不是"把敏感信息抠掉"，而是"本地模型直接处理原始敏感数据，
# 安全边界由部署位置（不出网）+ 权限过滤 + 审计留痕保证。
REDACT = os.environ.get("REDACT_PII", "0") == "1"
QUESTIONS = [
    "本公司的控股股东是哪家公司？持股比例多少？",
    "这次关联交易的金额是多少？交易的标的是什么？",
    "已辞任的财务总监是谁？他的联系方式是什么？",
]


def patch_embeddings():
    """把硬编码的英文 embedding 换成中文模型（走 HuggingFace，已验证可用）。"""
    import guardrag.rag.core as core
    from langchain_huggingface import HuggingFaceEmbeddings

    core._embeddings = HuggingFaceEmbeddings(
        model_name="BAAI/bge-small-zh-v1.5",
        model_kwargs={"device": "cpu"},
        encode_kwargs={"normalize_embeddings": True},
    )
    print("[emb] HuggingFace BAAI/bge-small-zh-v1.5（中文，512 维）")
    return "bge-small-zh-v1.5"


def build(rebuild: bool):
    from guardrag import build_rag_chain, load_stored_rag_chain

    meta = STORAGE / "_meta.json"
    if not rebuild and meta.exists():
        import json

        try:
            data = json.loads(meta.read_text(encoding="utf-8"))
            db_id = list(data.keys())[0] if isinstance(data, dict) and data else None
            if db_id:
                print(f"[db] 复用已有库 {db_id}")
                return db_id, load_stored_rag_chain(db_id=db_id, model=MODEL, storage_dir=str(STORAGE))
        except Exception as e:
            print(f"[db] 复用失败({e})，重建")

    print(f"[db] 建库中… 文档={DOC.name} 模型={MODEL}")
    t0 = time.time()
    db_id, chain = build_rag_chain(
        file_paths=[str(DOC)],
        model=MODEL,
        chunk_size=800,
        chunk_overlap=150,
        storage_dir=str(STORAGE),
        redact_pii=REDACT,        # 默认关闭：本地模型直接读原文，安全靠部署边界+权限+审计
    )
    print(f"[db] 完成 db_id={db_id} 耗时 {time.time()-t0:.1f}s")
    return db_id, chain


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rebuild", action="store_true")
    a = ap.parse_args()

    emb = patch_embeddings()
    db_id, chain = build(a.rebuild)

    from guardrag.utils.safety import check_input_safety, check_output_safety

    print("\n" + "=" * 70)
    for i, q in enumerate(QUESTIONS, 1):
        blocked = check_input_safety(q, sensitivity=SENSITIVITY)
        if blocked:
            print(f"\nQ{i}: {q}\n   [输入被护栏拦截] {blocked}")
            continue
        t0 = time.time()
        try:
            resp = chain.invoke({"input": q, "chat_history": []})
        except Exception as e:
            print(f"\nQ{i}: {q}\n   [问答失败] {type(e).__name__}: {e}")
            continue
        dt = time.time() - t0
        answer = resp.get("answer", resp) if isinstance(resp, dict) else str(resp)
        out_block = check_output_safety(str(answer), sensitivity=SENSITIVITY)
        print(f"\nQ{i}: {q}   ({dt:.1f}s)")
        print(f"  A: {answer}")
        ctx = resp.get("context", []) if isinstance(resp, dict) else []
        for c in ctx[:2]:
            src = getattr(c, "metadata", {}).get("source", "?")
            txt = getattr(c, "page_content", str(c))[:160].replace("\n", " ")
            print(f"  ├ 引用[{src}]: {txt}")
        if out_block:
            print(f"  ⚠ 输出护栏: {out_block}")

    print("\n" + "=" * 70)
    print(f"embedding = {emb} | model = {MODEL} | sensitivity = {SENSITIVITY}")
    print(f"存储目录 = {STORAGE}")


if __name__ == "__main__":
    main()
