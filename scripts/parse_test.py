"""真实港股公告解析质量实测：文本提取 + 表格提取能不能用。

评估三个指标：
  1. 文本提取完整性（字符数、有没有乱码、繁简是否保留）
  2. 表格识别数（pdfplumber 能认出几个表）
  3. 表格结构化质量（表头和数据行对不对齐——这是 RAG 检索的关键）
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import fitz          # pymupdf
import pdfplumber

PDFS = sorted(Path("data/real_hkex").glob("*.pdf"))


def text_stats(p: Path) -> dict:
    doc = fitz.open(p)
    text = "\n".join(page.get_text() for page in doc)
    lines = [l for l in text.splitlines() if l.strip()]
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    return {
        "pages": len(doc),
        "chars": len(text),
        "lines": len(lines),
        "cjk_ratio": round(cjk / max(len(text), 1), 3),
        "sample": lines[0][:40] if lines else "",
    }


def table_stats(p: Path) -> dict:
    tables = []
    with pdfplumber.open(p) as pdf:
        for i, page in enumerate(pdf.pages, 1):
            try:
                for t in page.extract_tables():
                    if not t or len(t) < 2:
                        continue
                    rows = len(t)
                    cols = max(len(r) for r in t)
                    filled = sum(1 for r in t for c in r if c and str(c).strip())
                    tables.append({
                        "page": i, "rows": rows, "cols": cols,
                        "filled_cells": filled,
                        "header": [str(c or "").strip()[:18] for c in t[0][:6]],
                        "first_data": [str(c or "").strip()[:18] for c in (t[1] if len(t) > 1 else [])[:6]],
                    })
            except Exception as e:
                tables.append({"page": i, "error": str(e)[:60]})
    return {"count": len(tables), "tables": tables}


def main() -> None:
    if not PDFS:
        print("没有 PDF，先下载到 data/real_hkex/")
        sys.exit(1)

    report = {}
    for p in PDFS:
        print(f"\n{'='*70}\n{p.name}")
        ts = text_stats(p)
        print(f"  文本: {ts['pages']} 页 / {ts['chars']:,} 字符 / 中文占比 {ts['cjk_ratio']}")
        print(f"  首行: {ts['sample']}")

        tt = table_stats(p)
        print(f"  表格: 识别出 {tt['count']} 个")
        for t in tt["tables"][:4]:
            if "error" in t:
                print(f"    第{t['page']}页 错误: {t['error']}")
                continue
            print(f"    第{t['page']}页 {t['rows']}行×{t['cols']}列 有效格{t['filled_cells']}")
            print(f"      表头: {t['header']}")
            print(f"      首行: {t['first_data']}")

        report[p.name] = {"text": ts, "tables": tt}

    Path("out").mkdir(exist_ok=True)
    Path("out/parse_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n完整报告: out/parse_report.json")


if __name__ == "__main__":
    main()
