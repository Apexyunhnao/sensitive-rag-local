"""按坐标重建 PDF 表格（无框线场景）。

港股公告的表格没有线，纯靠空白对齐。pymupdf 的默认文本流会把数字一行一个拆散，
模型看到"673"不知道是哪个协议哪一年。这里用 word 的 bbox 按 y 聚行、按 x 排序重建。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pymupdf


def rebuild_table(pdf: Path, page_no: int, y_from: float = 0, y_to: float = 9999) -> list[str]:
    doc = pymupdf.open(pdf)
    page = doc[page_no - 1]
    words = page.get_text("words")     # (x0, y0, x1, y1, word, block, line, word_no)
    rows: dict[float, list] = {}
    for w in words:
        x0, y0, x1, y1, txt = w[0], w[1], w[2], w[3], w[4]
        if not (y_from <= y0 <= y_to):
            continue
        # 同一行的判定：y 中心差 < 6pt
        key = round(y0 / 6) * 6
        rows.setdefault(key, []).append((x0, txt))

    out = []
    for y in sorted(rows):
        cells = sorted(rows[y], key=lambda c: c[0])
        line = "  ".join(t for _, t in cells)
        if line.strip():
            out.append(f"{y:6.0f} │ {line}")
    return out


def show(pdf: Path, page_no: int, y_from: float, y_to: float, label: str) -> None:
    print(f"\n=== {label}（{pdf.name} 第{page_no}页 y={y_from:.0f}-{y_to:.0f}）===")
    for line in rebuild_table(pdf, page_no, y_from, y_to):
        print(line)


if __name__ == "__main__":
    p = Path("data/real_hkex/cnooc_framework.pdf")
    # 第 4 页的历史交易金额表大致范围
    for line in rebuild_table(p, 4):
        print(line)
