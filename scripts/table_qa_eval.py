"""表格问答对照评测（可复跑）：同一份公告、同一个模型，比较两种喂法的逐题成败。

背景（README 的核心卖点）：港股公告的无框线表格被 pdfplumber 识别为 0 个表，
压平成裸数字后模型分不清「哪一年 / 哪一项」。按坐标重建、把列名写进每一行，
人工对比是 4/4 vs 3/4 —— 但那是单次人工对比，没有可复跑脚本。

本脚本补的就是这块证据：
  · 4 份公告 × 2 种喂法（markdown 压平 / 事实句）× N 次重复 × 固定温度与 seed
  · 逐题记录：模型原始回答、抽出的数字、是否命中真值、是否命中干扰值
  · 落盘 JSON + Markdown 报告，任何人可复跑

用法：
    python3 scripts/table_qa_eval.py                # 全量 4 题 × 2 喂法 × 5 次
    python3 scripts/table_qa_eval.py --repeats 3    # 改重复次数
    python3 scripts/table_qa_eval.py --case cnooc   # 只跑一题
    python3 scripts/table_qa_eval.py --dry-run      # 只打印两种喂法的输入，不调模型

依赖：本地 Ollama（默认 127.0.0.1:11434）+ qwen2.5:7b；纯标准库发请求。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "out" / "table_qa_eval"
OLLAMA = "http://127.0.0.1:11434/api/chat"
MODEL = "qwen2.5:7b"

# ── 评测集：4 份公告各 1 道金额题，真值以 PDF 表格原始数值为准 ──
# 每题的 columns / rows 是从 PDF 按 word bbox 的 x 坐标对齐后人工核过的（坐标见 rebuild_table.py 同款逻辑）。
CASES = [
    {
        "id": "qise-p10",
        "pdf": "data/real_hkex/acquisition_qise.pdf",
        "page": 10,
        "title": "七色珠光综合财务资料（3 列表格）",
        "question": "七色珠光「截至二零二四年十二月三十一日止年度」的税前净利润是多少？只回答数字和单位。",
        "truth": "539,537.8",
        "distractors": ["317,567.8", "289,775.8"],
        "unit": "人民币千元",
        "columns": ["截至二零二三年十二月三十一日止年度", "截至二零二四年十二月三十一日止年度",
                    "截至二零二五年六月三十日止六个月"],
        "rows": [
            ("税前净利润", ["317,567.8", "539,537.8", "289,775.8"]),
            ("税后净利润", ["280,309.8", "469,527.4", "246,757.3"]),
        ],
        "missing_note": None,
    },
    {
        "id": "chalco-p6",
        "pdf": "data/real_hkex/chalco_jv.pdf",
        "page": 6,
        "title": "驰宏锗业净利润（2 列表格，列序为 2024 在前、2023 在后）",
        "question": "驰宏锗业「截至2024年12月31日止年度」扣除税项及非经常性项目之前的净利润是多少？只回答数字和单位。",
        "truth": "709.86",
        "distractors": ["688.98", "568.33", "475.81"],
        "unit": "人民币万元",
        "columns": ["截至2024年12月31日止年度", "截至2023年12月31日止年度"],
        "rows": [
            ("扣除税项及非经常性项目之前的净利润", ["709.86", "688.98"]),
            ("扣除税项及非经常性项目之后的净利润", ["568.33", "475.81"]),
        ],
        "missing_note": None,
    },
    {
        "id": "cnooc-p4",
        "pdf": "data/real_hkex/cnooc_framework.pdf",
        "page": 4,
        "title": "中海油服持续关连交易年度上限（7 列表头，2025H1 一列无数据）",
        "question": "「中國海油集團向本集團提供物業服務」項下，截至2026年12月31日止年度的建議年度上限是多少？只回答数字和单位。",
        "truth": "363",
        "distractors": ["429", "484", "804", "734"],
        "unit": "人民币百万元",
        "columns": ["截至2023年12月31日止年度", "截至2024年12月31日止年度", "截至2025年12月31日止年度",
                    "截至2025年6月30日止六个月", "截至2026年12月31日止年度", "截至2027年12月31日止年度",
                    "截至2028年12月31日止年度"],
        # ⚠️ 2025H1 列按原文**无数据** ⇒ 这是「缺列错位」的核心案例
        "rows": [
            ("油田服务｜建议年度上限", ["45,104", "47,478", "49,925", None, "48,400", "52,800", "57,200"]),
            ("装备租赁等辅助服务｜建议年度上限", ["6,256", "6,837", "7,496", None, "4,747", "5,411", "7,066"]),
            ("物业服务｜建议年度上限", ["673", "734", "804", None, "363", "429", "484"]),
        ],
        "missing_note": "注意：截至2025年6月30日止六个月一列在原表中没有数值",
    },
    {
        "id": "luxshare-p4",
        "pdf": "data/real_hkex/continuing_luxshare.pdf",
        "page": 4,
        "title": "立讯精密总供应协议年度上限（正文型，非表格）",
        "question": "截至二零二七年十二月三十一日止年度的现年度上限，被修订为多少港元？只回答数字和单位。",
        "truth": "1,758,000,000",
        "distractors": ["240,000,000", "1,598,000,000", "1,934,000,000"],
        "unit": "港元",
        "columns": ["截至二零二六年十二月三十一日止年度", "截至二零二七年十二月三十一日止年度",
                    "截至二零二八年十二月三十一日止年度"],
        "rows": [
            ("修订前年度上限", ["240,000,000", "240,000,000", None]),
            ("修订后年度上限", ["1,598,000,000", "1,758,000,000", "1,934,000,000（建议）"]),
        ],
        "missing_note": "原文另有一句「並將…年期延長至截至二零二八年…建議年度上限為1,934,000,000港元」",
    },
]


def build_markdown(case: dict) -> str:
    """喂法 A：压平成 Markdown 表格（模型只能靠数第几列对齐）。"""
    cols = case["columns"]
    lines = ["| 项目 | " + " | ".join(cols) + " |",
             "|---|" + "---|" * len(cols)]
    for name, vals in case["rows"]:
        cells = [(v if v is not None else "") for v in vals]
        lines.append("| " + name + " | " + " | ".join(cells) + " |")
    body = "\n".join(lines)
    return f"以下是《{case['title']}》的表格（单位：{case['unit']}）：\n\n{body}\n"


def build_facts(case: dict) -> str:
    """喂法 B：每一格拆成事实句（列名写进每一行）。"""
    out = [f"以下是《{case['title']}》的表格事实（单位：{case['unit']}），一行一条："]
    for name, vals in case["rows"]:
        for col, val in zip(case["columns"], vals):
            if val is None:
                continue
            out.append(f"- {name} ｜ {col} ｜ {case['unit']} {val}")
        # 缺失列也显式说明，避免模型误认为「少一个数 = 少一列不存在」
        if case.get("missing_note"):
            out.append(f"- （{name}：{case['missing_note']}）")
    return "\n".join(out) + "\n"


def build_flat(case: dict) -> str:
    """喂法 C：模拟生产里真实拿到的 PDF 文本流 —— 数字连成一行、列名不在行内。

    这是 pdfplumber/pymupdf 对无框线表格的实际输出形态：表头一行，数值挤在后面，
    空列会被直接跳过（这正是「缺列时数错位置」的成因）。
    """
    head = "  ".join(case["columns"])
    lines = [f"以下是《{case['title']}》的原文文本（单位：{case['unit']}）：", "", head]
    for name, vals in case["rows"]:
        present = [v for v in vals if v is not None]      # ⚠️ 空列被吃掉，不做占位
        lines.append(f"{name}  " + "  ".join(present))
    return "\n".join(lines) + "\n"


def ask(question: str, context: str, temperature: float = 0.0, seed: int = 42) -> str:
    payload = {
        "model": MODEL,
        "stream": False,
        "options": {"temperature": temperature, "seed": seed, "num_predict": 120},
        "messages": [
            {"role": "system",
             "content": "你是严谨的财务数据提取助手。只根据给定材料回答，只输出一个数字加单位，"
                        "不要解释，不要复述题目。"},
            {"role": "user", "content": f"{context}\n\n问题：{question}"},
        ],
    }
    req = urllib.request.Request(
        OLLAMA,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        data = json.loads(r.read().decode("utf-8"))
    return (data.get("message") or {}).get("content", "").strip()


def norm_numbers(text: str) -> list[str]:
    """抽出答案里的数字（去掉千分位逗号），按出现顺序返回。"""
    raw = re.findall(r"\d[\d,]*(?:\.\d+)?", text or "")
    return [x.replace(",", "") for x in raw]


def judge(answer: str, truth: str, distractors: list[str]) -> dict:
    got = norm_numbers(answer)
    t = truth.replace(",", "")
    ds = [d.replace(",", "") for d in distractors]
    hit_truth = t in got
    hit_distractor = [d for d in ds if d in got]
    if hit_truth and not hit_distractor:
        verdict = "correct"
    elif hit_truth and hit_distractor:
        verdict = "correct_but_leaked_distractor"
    elif hit_distractor:
        verdict = "wrong_displaced"
    elif not got:
        verdict = "no_number"
    else:
        verdict = "wrong_other"
    return {"numbers": got, "hit_truth": hit_truth,
            "hit_distractors": hit_distractor, "verdict": verdict}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeats", type=int, default=5, help="每个组合重复次数（默认 5）")
    ap.add_argument("--temperature", type=float, default=0.0)
    ap.add_argument("--case", default=None, help="只跑某个 case id（如 cnooc-p4）")
    ap.add_argument("--dry-run", action="store_true", help="只打印两种喂法，不调模型")
    args = ap.parse_args()

    cases = [c for c in CASES if args.case is None or c["id"] == args.case or
             c["id"].startswith(args.case)]
    if not cases:
        print(f"没有匹配的 case：{args.case}（可选：{', '.join(c['id'] for c in CASES)}）")
        sys.exit(2)

    if args.dry_run:
        for c in cases:
            print("=" * 70)
            print(c["id"], "|", c["title"])
            print("\n--- 喂法 A（markdown 压平）---\n" + build_markdown(c))
            print("\n--- 喂法 B（事实句）---\n" + build_facts(c))
            print("\n--- 喂法 C（PDF 文本流，空列被吃掉）---\n" + build_flat(c))
        return

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    rows = []
    t0 = time.time()

    for c in cases:
        for mode, builder in (("markdown", build_markdown), ("facts", build_facts), ("flat", build_flat)):
            ctx = builder(c)
            for i in range(args.repeats):
                seed = 42 + i
                try:
                    ans = ask(c["question"], ctx, args.temperature, seed)
                    err = None
                except Exception as e:      # 网络 / 模型不可用 → 记失败，不中断整轮
                    ans, err = "", repr(e)
                j = judge(ans, c["truth"], c["distractors"])
                rows.append({
                    "case": c["id"], "mode": mode, "repeat": i + 1, "seed": seed,
                    "truth": c["truth"], "unit": c["unit"], "question": c["question"],
                    "answer": ans, "error": err, **j,
                })
                mark = {"correct": "✅", "wrong_displaced": "❌错位",
                        "correct_but_leaked_distractor": "⚠️同时出现干扰值",
                        "no_number": "❌无数字", "wrong_other": "❌其它"}[j["verdict"]]
                print(f"  {c['id']:14s} {mode:9s} #{i+1} {mark:12s} ans={ans[:40]!r}")

    elapsed = time.time() - t0
    report = {
        "generated_at": stamp, "model": MODEL, "temperature": args.temperature,
        "repeats": args.repeats, "elapsed_seconds": round(elapsed, 1),
        "cases": [{k: c[k] for k in ("id", "pdf", "page", "title", "question", "truth",
                                     "distractors", "unit")} for c in cases],
        "rows": rows,
        "summary": summarize(rows, cases),
    }
    jp = OUT_DIR / f"report-{stamp}.json"
    jp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md = OUT_DIR / f"report-{stamp}.md"
    md.write_text(render_md(report), encoding="utf-8")

    print("\n" + render_md(report))
    print(f"\n落盘：{jp}\n      {md}")


def summarize(rows: list, cases: list) -> dict:
    """按「喂法 × 题目」统计：正确次数 / 总次数；另给按喂法的合计。"""
    by = {}
    for r in rows:
        key = (r["mode"], r["case"])
        s = by.setdefault(key, {"total": 0, "correct": 0, "displaced": 0})
        s["total"] += 1
        if r["verdict"] == "correct":
            s["correct"] += 1
        if r["verdict"] == "wrong_displaced":
            s["displaced"] += 1
    out = {"by_mode_case": {}, "by_mode": {}}
    for (mode, case), s in by.items():
        out["by_mode_case"].setdefault(mode, {})[case] = s
    for mode in ("markdown", "facts", "flat"):
        tot = sum(v["total"] for k, v in by.items() if k[0] == mode)
        ok = sum(v["correct"] for k, v in by.items() if k[0] == mode)
        out["by_mode"][mode] = {"correct": ok, "total": tot,
                                "rate": round(ok / tot * 100, 1) if tot else None}
    return out


def render_md(report: dict) -> str:
    s = report["summary"]
    lines = [f"# 表格问答对照评测 {report['generated_at']}", "",
             f"- 模型：`{report['model']}` · 温度 {report['temperature']} · 每题重复 {report['repeats']} 次 · "
             f"耗时 {report['elapsed_seconds']}s",
             "- 三种喂法：`markdown` = 压平成 markdown 表格（完整保留空列）；"
             "`facts` = 每格拆成「项目｜列名｜单位 数值」事实句；"
             "`flat` = 模拟生产里的 PDF 文本流（空列被吃掉、数字连成一行）", "",
             "## 合计", "", "| 喂法 | 答对 / 总数 | 正确率 |", "|---|---|---|"]
    for mode in ("markdown", "facts", "flat"):
        v = s["by_mode"][mode]
        lines.append(f"| {mode} | {v['correct']} / {v['total']} | {v['rate']}% |")
    lines += ["", "## 逐题（正确次数 / 总次数）", "",
              "| 题目 | markdown | facts | flat | 真值 |", "|---|---|---|---|---|"]
    for c in report["cases"]:
        m = s["by_mode_case"].get("markdown", {}).get(c["id"], {})
        f = s["by_mode_case"].get("facts", {}).get(c["id"], {})
        fl = s["by_mode_case"].get("flat", {}).get(c["id"], {})
        lines.append(f"| {c['id']} | {m.get('correct', 0)}/{m.get('total', 0)} | "
                     f"{f.get('correct', 0)}/{f.get('total', 0)} | {fl.get('correct', 0)}/{fl.get('total', 0)} | "
                     f"{c['truth']} {c['unit']} |")
    # 错位明细
    disp = [r for r in report["rows"] if r["verdict"] == "wrong_displaced"]
    lines += ["", "## 错位明细（答出了别的年份/别的项目的值）", ""]
    if not disp:
        lines.append("无")
    else:
        lines.append("| 题目 | 喂法 | # | 回答 | 命中干扰值 |")
        lines.append("|---|---|---|---|---|")
        for r in disp:
            lines.append(f"| {r['case']} | {r['mode']} | {r['repeat']} | {r['answer'][:40]} | "
                         f"{', '.join(r['hit_distractors'])} |")
    errs = [r for r in report["rows"] if r["error"]]
    if errs:
        lines += ["", f"⚠️ 有 {len(errs)} 次调用失败（模型/网络），已记在 JSON 的 error 字段。"]
    lines += ["", "> 复跑：`python3 scripts/table_qa_eval.py`（需本地 Ollama + qwen2.5:7b）。"]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    main()
