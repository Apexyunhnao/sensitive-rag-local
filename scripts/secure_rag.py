"""带权限过滤与审计留痕的本地 RAG（敏感数据不出网）。

设计要点：
1. 权限在**检索层**生效 —— 无权文档根本不进候选集，不是查出来再隐藏。
2. 审计只记**行为** —— 谁、何时、命中哪些文档、是否被拦截；
   不存问题原文、不存回答全文（合规要求：日志本身不能变成泄露源）。
3. 全程本地：embedding 与 LLM 都走本机，配合 net_audit.py 可证明零外连。

用法：
    python scripts/secure_rag.py --build          # 建库（带权限元数据）
    python scripts/secure_rag.py --ask zhang "控股股东是谁"
    python scripts/secure_rag.py --audit          # 看审计日志
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

# ---------- 环境（必须在 import 框架前设置）----------
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_OFFLINE", "1")          # 模型已缓存，强制离线
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")
os.environ.setdefault("DO_NOT_TRACK", "1")

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
STORE = ROOT / "out" / "secure_store"
AUDIT_DB = ROOT / "out" / "audit.db"
EMB_MODEL = "BAAI/bge-small-zh-v1.5"
LLM_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:7b")


# ---------- 权限模型 ----------
@dataclass
class Doc:
    doc_id: str
    path: Path
    groups: list[str] = field(default_factory=list)


# 文档 → 可见组。真实系统从 ACL 表/目录服务读，这里用最小配置演示。
DOC_ACL: dict[str, list[str]] = {
    "hkex_announcement_2026_041.md": ["compliance", "board"],
    "internal_audit_2026_q3.md": ["compliance", "audit"],
    "hr_roster.md": ["hr"],
}

# 用户 → 所属组
USER_GROUPS: dict[str, list[str]] = {
    "zhang": ["compliance"],          # 合规部：能看公告 + 内审
    "wang": ["board"],                # 董事：能看公告
    "li": ["hr"],                     # 人事：只能看花名册
    "guest": [],                      # 无权限：什么都看不到
}


def visible_docs(user: str) -> set[str]:
    """用户能看到的文档集合。"""
    groups = set(USER_GROUPS.get(user, []))
    return {d for d, allowed in DOC_ACL.items() if groups & set(allowed)}


# ---------- 审计 ----------
def audit_init() -> None:
    AUDIT_DB.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(AUDIT_DB, timeout=10) as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS audit (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   ts TEXT NOT NULL,
                   user TEXT NOT NULL,
                   action TEXT NOT NULL,
                   query_hash TEXT NOT NULL,
                   query_len INTEGER NOT NULL,
                   doc_ids TEXT NOT NULL,
                   n_results INTEGER NOT NULL,
                   blocked INTEGER NOT NULL,
                   latency_ms INTEGER NOT NULL
               )"""
        )


def audit_write(user: str, action: str, query: str, doc_ids: list[str],
                n_results: int, blocked: bool, latency_ms: int) -> None:
    """只记行为，不记原文 —— 问题只留哈希与长度，无法还原内容。"""
    qh = hashlib.sha256(query.encode("utf-8")).hexdigest()[:16]
    with sqlite3.connect(AUDIT_DB, timeout=10) as c:
        c.execute(
            "INSERT INTO audit (ts,user,action,query_hash,query_len,doc_ids,n_results,blocked,latency_ms)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (time.strftime("%Y-%m-%d %H:%M:%S"), user, action, qh, len(query),
             json.dumps(doc_ids, ensure_ascii=False), n_results, int(blocked), latency_ms),
        )


# ---------- 建库 ----------
def get_embeddings():
    from langchain_huggingface import HuggingFaceEmbeddings
    return HuggingFaceEmbeddings(
        model_name=EMB_MODEL,
        model_kwargs={"device": "cpu"},
        encode_kwargs={"normalize_embeddings": True},
    )


def build() -> None:
    from langchain_community.vectorstores import FAISS
    from langchain_text_splitters import RecursiveCharacterTextSplitter

    splitter = RecursiveCharacterTextSplitter(chunk_size=800, chunk_overlap=150)
    texts, metas, ids = [], [], []

    for doc_id, groups in DOC_ACL.items():
        p = DATA / doc_id
        if not p.exists():
            print(f"  [skip] {doc_id} 不存在")
            continue
        content = p.read_text(encoding="utf-8")
        chunks = splitter.split_text(content)
        for i, ch in enumerate(chunks):
            texts.append(ch)
            # groups 存成逗号串：FAISS 的 metadata 用简单标量更稳，
            # 过滤时按子串边界匹配（见 _filter_for）
            metas.append({"doc_id": doc_id, "groups": "|".join(groups)})
            ids.append(f"{doc_id}::{i}")
        print(f"  {doc_id}: {len(chunks)} 块 → 可见组 {groups}")

    if not texts:
        sys.exit("没有可建库的文档")

    STORE.mkdir(parents=True, exist_ok=True)
    vs = FAISS.from_texts(texts, get_embeddings(), metadatas=metas, ids=ids)
    vs.save_local(str(STORE))
    print(f"[build] {len(texts)} 块 → {STORE}")


# ---------- 检索（权限在这一层生效）----------
def load_store():
    from langchain_community.vectorstores import FAISS
    if not (STORE / "index.faiss").exists():
        sys.exit("库不存在，先跑 --build")
    return FAISS.load_local(str(STORE), get_embeddings(), allow_dangerous_deserialization=True)


def _filter_for(user: str):
    """构造 metadata 过滤器：只放行用户可见组的内容。

    用闭包而不是 dict filter —— FAISS 的 dict filter 只能做等值匹配，
    权限是「集合求交」，必须用 callable。
    """
    allowed = set(USER_GROUPS.get(user, []))
    if not allowed:
        return lambda m: False          # 无组 = 什么都看不到

    def _f(meta: dict) -> bool:
        groups = set((meta.get("groups") or "").split("|"))
        return bool(groups & allowed)

    return _f


def ask(user: str, question: str, k: int = 4) -> str:
    from langchain_ollama import ChatOllama

    t0 = time.time()
    vs = load_store()

    # 关键：filter 在检索时生效，无权内容不进入候选集
    docs = vs.similarity_search(question, k=k, filter=_filter_for(user))
    hit_docs = sorted({d.metadata.get("doc_id", "?") for d in docs})

    if not docs:
        latency = int((time.time() - t0) * 1000)
        audit_write(user, "search", question, [], 0, blocked=True, latency_ms=latency)
        return f"[拒绝] 用户 {user} 无可访问的匹配内容（已记审计）。"

    context = "\n\n---\n\n".join(
        f"[{d.metadata.get('doc_id')}] {d.page_content}" for d in docs
    )
    prompt = (
        "根据以下资料回答问题。只用资料中的信息，资料没有就说未提及。"
        "回答简洁，末尾标注来源文档名。\n\n"
        f"资料：\n{context}\n\n问题：{question}"
    )
    llm = ChatOllama(model=LLM_MODEL, temperature=0)
    answer = llm.invoke(prompt).content

    latency = int((time.time() - t0) * 1000)
    audit_write(user, "search", question, hit_docs, len(docs), blocked=False,
                latency_ms=latency)
    return f"{answer}\n\n[来源: {', '.join(hit_docs)}] [{latency}ms]"


def show_audit(limit: int = 20) -> None:
    if not AUDIT_DB.exists():
        print("无审计记录")
        return
    with sqlite3.connect(AUDIT_DB, timeout=10) as c:
        rows = c.execute(
            "SELECT ts,user,action,query_hash,query_len,doc_ids,n_results,blocked,latency_ms"
            " FROM audit ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    print(f"{'时间':<20}{'用户':<8}{'命中文档':<38}{'结果':<6}{'拦截':<5}{'ms'}")
    print("-" * 86)
    for ts, user, action, qh, qlen, doc_ids, n, blocked, ms in rows:
        print(f"{ts:<20}{user:<8}{doc_ids:<38}{n:<6}{'是' if blocked else '否':<5}{ms}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", action="store_true", help="建库")
    ap.add_argument("--ask", nargs=2, metavar=("USER", "QUESTION"), help="以某用户身份提问")
    ap.add_argument("--audit", action="store_true", help="查看审计日志")
    ap.add_argument("--whoami", metavar="USER", help="查看某用户可见文档")
    args = ap.parse_args()

    audit_init()

    if args.build:
        build()
    elif args.ask:
        user, q = args.ask
        print(f"[{user}] {q}")
        print(ask(user, q))
    elif args.audit:
        show_audit()
    elif args.whoami:
        print(f"{args.whoami} 可见: {sorted(visible_docs(args.whoami)) or '(无)'}")
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
