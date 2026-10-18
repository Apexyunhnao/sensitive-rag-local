"""把员工上传的内容写进对应密级的 Dify 知识库，并支持撤销。

为什么单独一个文件：写入是「副作用」动作，出问题会污染知识库；
和飞书接入层分开，便于单独测试、单独限制。

用法（自测，会真的写一个文档再删掉）：
    python scripts/dify_ingest.py --selftest

在代码里：
    ok, doc_id, msg = add_text("合规知识库", "员工上传-身份证.txt", "……")
    ok2, msg2 = delete_document("合规知识库", doc_id)
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASE = "http://127.0.0.1"
TIERS_FILE = ROOT / ".dify-tiered.json"


def _tiers() -> dict:
    return json.loads(TIERS_FILE.read_text(encoding="utf-8"))


def _call(method: str, path: str, key: str, payload: dict | None = None) -> tuple[int, dict | str]:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        f"{BASE}{path}", data=data, method=method,
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode()[:300]
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, raw
    except Exception as e:  # noqa: BLE001
        return 0, f"连接失败: {e}"


def add_text(tier: str, name: str, text: str) -> tuple[bool, str, str]:
    """写入一个纯文本文档。返回 (成功, document_id, 说明)。

    为什么要「先 OCR 再写文本」而不是直接传图片：图片进 Dify 只能靠它的
    内置解析，而我们的表格/证件解析是自写的（pdf_to_md / RapidOCR），
    先抽成结构化文本再写，检索质量可控，也便于人工确认。
    """
    tiers = _tiers()
    if tier not in tiers:
        return False, "", f"未知密级 {tier}"
    cfg = tiers[tier]
    status, resp = _call(
        "POST", f"/v1/datasets/{cfg['dataset_id']}/document/create-by-text",
        cfg["dataset_key"],
        {"name": name, "text": text,
         "indexing_technique": "high_quality",
         "process_rule": {"mode": "automatic"}})
    if status not in (200, 201):
        detail = resp if isinstance(resp, str) else json.dumps(resp, ensure_ascii=False)[:200]
        return False, "", f"写入失败 HTTP {status}: {detail}"
    doc = (resp or {}).get("document", {}) if isinstance(resp, dict) else {}
    doc_id = doc.get("id", "")
    if not doc_id:
        return False, "", f"写入返回无 document.id: {json.dumps(resp, ensure_ascii=False)[:200]}"
    return True, doc_id, f"已写入「{tier}」文档 {doc.get('name', name)}"


def delete_document(tier: str, doc_id: str) -> tuple[bool, str]:
    """撤销：从知识库删除该文档（含其分段）。"""
    tiers = _tiers()
    if tier not in tiers:
        return False, f"未知密级 {tier}"
    cfg = tiers[tier]
    status, resp = _call(
        "DELETE", f"/v1/datasets/{cfg['dataset_id']}/documents/{doc_id}",
        cfg["dataset_key"])
    if status in (200, 204):
        return True, "已删除"
    detail = resp if isinstance(resp, str) else json.dumps(resp, ensure_ascii=False)[:200]
    return False, f"删除失败 HTTP {status}: {detail}"


def count_documents(tier: str) -> int:
    """当前库里有几个文档（用于验证写入/撤销是否真的生效）。"""
    cfg = _tiers()[tier]
    status, resp = _call("GET", f"/v1/datasets/{cfg['dataset_id']}/documents?page=1&limit=1",
                         cfg["dataset_key"])
    if status != 200 or not isinstance(resp, dict):
        return -1
    return int(resp.get("total", -1))


def selftest() -> int:
    """真写一个文档 → 数一下 → 删掉 → 再数一下。不通过就是写入链路有问题。"""
    tier = "合规知识库"
    passed, failed = [], []

    def check(name: str, cond: bool, detail: str = "") -> None:
        (passed if cond else failed).append(name)
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))

    print("== 写入链路自测 ==")
    before = count_documents(tier)
    print(f"  写入前文档数: {before}")
    check("能读到文档总数", before >= 0, str(before))

    marker = f"SELFTEST-{int(time.time())}"
    ok, doc_id, msg = add_text(tier, f"自测-{marker}.txt",
                              f"[{marker}] 这是一条写入链路自测记录，用于验证 create-by-text 与撤销。")
    check("create-by-text 成功", ok, msg)
    check("拿到 document_id", bool(doc_id), doc_id[:16] if doc_id else "")

    if ok and doc_id:
        time.sleep(2)  # 让 Dify 有时间落库
        mid = count_documents(tier)
        check("文档数 +1", mid == before + 1, f"{before} → {mid}")

        ok2, msg2 = delete_document(tier, doc_id)
        check("撤销成功", ok2, msg2)
        time.sleep(2)
        after = count_documents(tier)
        check("文档数回到原值", after == before, f"{mid} → {after}")

    print(f"\n通过 {len(passed)} 项，失败 {len(failed)} 项")
    if failed:
        print("失败项:", failed)
    return 0 if not failed else 1


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--count", metavar="TIER", help="数一下某个库有几个文档")
    a = ap.parse_args()
    if a.selftest:
        sys.exit(selftest())
    if a.count:
        print(f"{a.count}: {count_documents(a.count)} 个文档")
        return
    ap.print_help()


if __name__ == "__main__":
    main()
