"""港股公告 PDF → 可检索文本（正文 + 表格事实句）。

为什么要转事实句：实测同一份公告、同样的 7B 模型
  · Markdown 表格喂进去  → 3/4 正确（缺列时模型会数错位置）
  · 拆成「项目 年份 数值」事实句 → 5/5 正确
原因：7B 从二维表格里按位置取数不可靠，但从一维句子里取数很可靠。

已知限制（不要吹成通用方案）：
  · 只处理「文字靠空白对齐」的表格，不处理有框线/合并单元格/跨页表格
  · 列名靠同行文字 x 坐标推断，遇到复杂表头仍需人工检查
  · 这是 200 行的应急实现，生产建议先用 MinerU 之类做版面分析

用法：
  python scripts/pdf_to_md.py data/real_hkex/cnooc_framework.pdf
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

import pymupdf

NUM_RE = re.compile(r"^[\d,]+(\.\d+)?%?$")


def is_num(t: str) -> bool:
    return bool(NUM_RE.match(t.replace(" ", "")))


def page_rows(page) -> list[tuple[float, list[tuple[float, str]]]]:
    """按 y 聚行 -> [(y, [(x, word), ...]), ...]。"""
    rows: dict[float, list[tuple[float, str]]] = {}
    for w in page.get_text("words"):
        rows.setdefault(round(w[1] / 6) * 6, []).append((w[0], w[4]))
    return [(y, sorted(rows[y])) for y in sorted(rows)]


def parse_header(row: list[tuple[float, str]]) -> list[tuple[float, str]]:
    """表头行 -> [(x起点, 列名)]。同名年份靠"止年度/止六個月"区分。"""
    cols: list[tuple[float, str]] = []
    buf: list[str] = []
    start_x = None
    for x, t in row:
        if re.search(r"20\d{2}\s*年", t):
            if buf and start_x is not None:
                cols.append((start_x, "".join(buf).strip()))
            buf, start_x = [t], x
        elif buf:
            buf.append(t)
    if buf and start_x is not None:
        cols.append((start_x, "".join(buf).strip()))
    return cols


def is_header_row(row: list[tuple[float, str]]) -> bool:
    return len(re.findall(r"20\d{2}\s*年", " ".join(t for _, t in row))) >= 2


def is_table_row(row: list[tuple[float, str]]) -> bool:
    """一行里至少 3 个纯数字单元格，才算数据行。"""
    nums = [t for _, t in row if is_num(t)]
    return len(nums) >= 3


def col_name(x: float, cols: list[tuple[float, str]]) -> str:
    if not cols:
        return ""
    return min(cols, key=lambda c: abs(c[0] - x))[1]


def extract(pdf: Path) -> str:
    doc = pymupdf.open(pdf)
    body: list[str] = [f"# {pdf.stem}", ""]
    facts: list[str] = []
    seen: set[str] = set()

    for pno, page in enumerate(doc, 1):
        rows = page_rows(page)
        if not rows:
            continue
        body.append(f"## 第 {pno} 页")
        body.append("")

        cols: list[tuple[float, str]] = []
        heading = ""
        prev_was_header = False

        for y, row in rows:
            texts = [(x, t) for x, t in row if not is_num(t)]
            nums = [(x, t) for x, t in row if is_num(t)]
            line = " ".join(t for _, t in row).strip()

            if is_header_row(row):
                newcols = parse_header(row)
                if prev_was_header and cols:
                    # 连续两行表头：把第二行的限定语并到最近的列上
                    for x, extra in newcols:
                        i = min(range(len(cols)), key=lambda k: abs(cols[k][0] - x))
                        cols[i] = (cols[i][0], cols[i][1] + extra)
                else:
                    cols = newcols
                prev_was_header = True
                body.append("表头：" + " | ".join(n for _, n in cols))
                body.append("")
                continue

            # 二级表头：紧跟表头行、无数字、含"止年度/止六個月"这类限定语
            if prev_was_header and not nums and ("止" in line) and len(line) <= 90:
                for x, t in texts:
                    i = min(range(len(cols)), key=lambda k: abs(cols[k][0] - x))
                    cols[i] = (cols[i][0], cols[i][1] + t)
                body.append("表头： " + line)
                body.append("")
                continue
            prev_was_header = False

            if not nums:
                # 项目名行：以 － 开头，或含"服務/協議/上限"且不长
                if 2 <= len(line) <= 45 and (line.startswith(("－", "-", "–"))
                                             or re.search(r"服務|協議|採購|租賃|物業", line)):
                    heading = line.strip("－-– 　")
                body.append(line)
                continue

            if not is_table_row(row):
                body.append(line)
                continue

            # 数据行：label = 项目名 + 行内前缀（前缀是"建議年度上限"这类时特别需要项目名）
            first_num_x = nums[0][0]
            prefix = "".join(t for x, t in texts if x < first_num_x).strip(" ：:　－-–")
            label = prefix or heading
            if heading and prefix and prefix != heading:
                label = f"{heading}｜{prefix}" if len(prefix) <= 16 else prefix
            if not label:
                body.append(line)
                continue

            body.append(line)
            for x, n in nums:
                cn = col_name(x, cols)
                if not cn:
                    continue
                fact = f"{label}｜{cn}｜{n}"
                if fact not in seen:
                    seen.add(fact)
                    facts.append(fact)

        body.append("")

    if facts:
        body.append("## 表格数据（事实句，供精确检索）")
        body.append("")
        body.extend(f"- {f}" for f in facts)
        body.append("")

    return "\n".join(body)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("pdfs", nargs="+")
    ap.add_argument("--out", default="out/facts")
    args = ap.parse_args()

    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    for p in map(Path, args.pdfs):
        md = extract(p)
        dest = outdir / (p.stem + ".md")
        dest.write_text(md, encoding="utf-8")
        n = md.count("\n- ")
        print(f"{p.name} → {dest}  ({len(md):,} 字符, {n} 条事实句)")


if __name__ == "__main__":
    main()
