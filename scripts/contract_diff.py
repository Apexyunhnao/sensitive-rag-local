"""合同版本比对：条款级切分 → 条款配对 → 条款内字符级 diff → 关键字段标注。

为什么不用「全文 difflib」一句话搞定：
  行级 diff 只能告诉你「这一段变了」，法务要的是「第 3 条付款期限从 30 个工作日
  改成 45 个工作日」。所以先把合同切成条款，条款配对后再在条款内部做字符级 diff，
  才能定位到具体字词。

为什么不用大模型做比对：
  比对是确定性运算，diff 算法能保证「不漏改动」。模型只负责把 diff 结果写成
  人话摘要——它要是漏了，人类根本没法发现。所以这里 0 模型。

用法：
    # 两个 PDF
    python scripts/contract_diff.py a.pdf b.pdf
    python scripts/contract_diff.py a.pdf b.pdf --json out/diff.json

    # 两个已经解析好的 markdown
    python scripts/contract_diff.py --md a.md --md2 b.md

    # 自测（程序内造一对合同，跑完断言）
    python scripts/contract_diff.py --selftest

输出：
    ① 控制台：按「关键变更优先」排的变更清单
    ② --json：结构化结果，含每处改动的原文/新文/上下文/分类标签
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from difflib import SequenceMatcher
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# ---------------------------------------------------------------- 条款切分

# 顺序有讲究：章 > 节 > 条 优先，否则「第 3 条第 2 款」会被反复切
CLAUSE_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("章", re.compile(r"^第\s*[一二三四五六七八九十百零〇\d]+\s*章")),
    ("节", re.compile(r"^第\s*[一二三四五六七八九十百零〇\d]+\s*节")),
    ("条", re.compile(r"^第\s*[一二三四五六七八九十百零〇\d]+\s*条")),
    ("paren", re.compile(r"^[（(][一二三四五六七八九十\d]+[）)]")),
    ("num", re.compile(r"^\d+(?:\.\d+)+\s*[.、]?\s")),   # 1.1 / 2.3.1
    ("num2", re.compile(r"^\d+[.、]\s*\S")),              # 1. / 2、
    ("cseq", re.compile(r"^[一二三四五六七八九十]+、")),
    ("article", re.compile(r"^Article\s+\d+", re.I)),
    ("clause", re.compile(r"^Clause\s+\d+", re.I)),
]

# PDF 解析产物里的页码/页眉噪音，不参与比对
NOISE_RE = re.compile(r"^(#|–\s*\d+\s*–|- ?\d+ ?-|第\s*\d+\s*页)")


def split_clauses(text: str) -> list[dict]:
    """按条款标记切块。没有标记的行归入当前条款正文。"""
    blocks: list[dict] = []
    cur = {"id": "前言", "kind": "head", "title": "前言", "body": []}

    for raw in text.split("\n"):
        line = raw.strip()
        if not line or NOISE_RE.match(line):
            continue

        hit = None
        for kind, rx in CLAUSE_PATTERNS:
            m = rx.match(line)
            if m:
                hit = (kind, m.group(0).strip())
                break

        if hit:
            if cur["title"] and (cur["body"] or cur["kind"] == "head"):
                blocks.append(cur)
            kind, marker = hit
            cur = {"id": marker, "kind": kind, "title": line, "body": []}
        else:
            cur["body"].append(line)

    if cur["title"] and (cur["body"] or cur["kind"] == "head"):
        blocks.append(cur)

    for b in blocks:
        b["text"] = "\n".join([b["title"]] + b["body"]).strip()
    return blocks


# ---------------------------------------------------------------- 条款配对


def title_words(block: dict) -> str:
    """去掉条款编号后的标题词，用于相似度比较。

    比整行更准：两行都以「第3条」开头时，光看整行会把不相干的条款判成相似的。
    """
    t = block["title"]
    for _, rx in CLAUSE_PATTERNS:
        m = rx.match(t)
        if m:
            t = t[m.end():]
            break
    return t.strip() or t


def pair_clauses(a: list[dict], b: list[dict]) -> tuple[list[tuple[int, int, str]], list[int], list[int]]:
    """全局贪心配对：所有候选对按综合分从高到低，一个个认领。

    为什么不能按编号配（实测踩过）：
      对方在中间插一条新条款，后面编号全部错位。
      按编号配就变成「第5条 争议解决」对上「第5条 违约责任」——全错。
      所以编号只给一点点加权，真正决定的是标题词 + 正文相似度。
    """
    if not a or not b:
        return [], list(range(len(b))), list(range(len(a)))

    cand: list[tuple[float, int, int]] = []
    for i, ca in enumerate(a):
        for j, cb in enumerate(b):
            ts = SequenceMatcher(None, title_words(ca), title_words(cb)).ratio()
            bs = SequenceMatcher(None, ca["text"], cb["text"]).ratio()
            score = 0.6 * ts + 0.4 * bs
            if ca["id"] == cb["id"]:
                score += 0.15          # 编号相同给甜头，但不是决定性的
            cand.append((score, i, j))
    cand.sort(reverse=True)

    used_a: set[int] = set()
    used_b: set[int] = set()
    pairs: list[tuple[int, int, str]] = []
    for score, i, j in cand:
        if score < 0.55:
            break
        if i in used_a or j in used_b:
            continue
        pairs.append((i, j, f"相似 {min(score, 1.0):.2f}"))
        used_a.add(i)
        used_b.add(j)

    pairs.sort()
    return pairs, [j for j in range(len(b)) if j not in used_b], [i for i in range(len(a)) if i not in used_a]


# ---------------------------------------------------------------- 字段标注

KEY_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("金额", re.compile(r"[0-9][0-9,.]*\s*(?:万元|亿元|元|港元|美元|人民币|USD|CNY|RMB|HKD)")),
    ("中文金额", re.compile(r"[零一二三四五六七八九十百千万亿]+(?:元|圆)")),
    ("日期", re.compile(r"\d{4}\s*年\s*\d{1,2}\s*月(?:\s*\d{1,2}\s*日)?|\d{4}[-/.]\d{1,2}[-/.]\d{1,2}")),
    ("百分比", re.compile(r"\d+(?:\.\d+)?\s*%")),
    ("期限", re.compile(r"\d+\s*(?:个工作日|工作日|个月|天|日|年)")),
    ("责任", re.compile(r"违约|赔偿|解除|终止|管辖|仲裁|诉讼|保密|不可抗力|连带")),
]

# 这一档的改动必须高亮：出错代价最高
CRITICAL_TAGS = {"金额", "中文金额", "日期", "百分比", "期限", "责任"}

# 关键字段的「整体」正则：日期、金额这些不能按字符比，否则
# 「二零二五年十一月二十八日 → 二零二六年一月十五日」会被拆成「五→六/删十/删二/八→五」
FIELD_PATTERNS: dict[str, list[re.Pattern]] = {
    "日期": [
        re.compile(r"[一二三四五六七八九十〇零]{2,4}\s*年\s*[一二三四五六七八九十]{1,2}\s*月"
                   r"(?:\s*[一二三四五六七八九十]{1,3}\s*日)?"),
        re.compile(r"\d{4}\s*[-/年.]\s*\d{1,2}\s*[-/月.]\s*\d{1,2}\s*日?"),
    ],
    "金额": [
        re.compile(r"[0-9][0-9,]*\.?[0-9]*\s*(?:万元|亿元|百萬|億|元|港元|美元|人民币|USD|CNY|RMB|HKD)"),
    ],
    "百分比": [re.compile(r"[0-9]+(?:\.[0-9]+)?\s*%")],
    "期限": [re.compile(r"[0-9]+\s*(?:个工作日|工作日|个月|天|日|年)")],
}


def extract_fields(text: str) -> dict[str, list[str]]:
    """按字段类型把关键数据整段抽出来（保持出现顺序）。"""
    out: dict[str, list[str]] = {}
    for name, rxs in FIELD_PATTERNS.items():
        vals: list[str] = []
        for rx in rxs:
            for m in rx.finditer(text):
                v = re.sub(r"\s+", " ", m.group(0)).strip()
                if v and v not in vals:
                    vals.append(v)
        if vals:
            out[name] = vals
    return out


def field_diff(old_text: str, new_text: str) -> list[dict]:
    """字段级对比：法务要的是「日期从 X 改成 Y」，不是「五改成六」。"""
    fa, fb = extract_fields(old_text), extract_fields(new_text)
    diffs: list[dict] = []
    for name in FIELD_PATTERNS:
        a, b = fa.get(name, []), fb.get(name, [])
        if a == b:
            continue
        gone = [x for x in a if x not in b]
        came = [x for x in b if x not in a]
        diffs.append({"field": name, "old": a, "new": b, "removed": gone, "added": came})
    return diffs


def tag_change(old: str, new: str, context: str = "") -> list[str]:
    """判断这处改动涉及哪些关键字段。

    必须看上下文：金额从 120 万改成 180 万时，字符级 diff 只框住「2」→「8」，
    光看这两个字符根本认不出是金额改动。
    """
    hay = f"{old} {new} {context}"
    return [name for name, rx in KEY_PATTERNS if rx.search(hay)]


def _expand_num(text: str, i1: int, i2: int) -> tuple[int, int]:
    """把纯数字的改动区间扩成完整数字：显示「2→8」没用，要看「120→180」。"""
    frag = text[i1:i2]
    if not frag or not re.fullmatch(r"[0-9.,]+", frag):
        return i1, i2
    while i1 > 0 and text[i1 - 1] in "0123456789.,":
        i1 -= 1
    while i2 < len(text) and text[i2] in "0123456789.,":
        i2 += 1
    return i1, i2


def char_diff(old: str, new: str, ctx: int = 26) -> list[dict]:
    """条款内部做字符级 diff，定位到具体字词。"""
    sm = SequenceMatcher(None, old, new)
    out: list[dict] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        o, n = old[i1:i2], new[j1:j2]
        # 纯空白变化不算改动
        if not o.strip() and not n.strip():
            continue
        if tag == "replace":       # 纯数字改动扩成完整数字，否则「2→8」没法读
            oi1, oi2 = _expand_num(old, i1, i2)
            nj1, nj2 = _expand_num(new, j1, j2)
            o, n = old[oi1:oi2], new[nj1:nj2]
        context = (old[max(0, i1 - ctx):i2 + ctx] + " … " +
                   new[max(0, j1 - ctx):j2 + ctx]).replace("\n", " ")
        tags = tag_change(o, n, context)
        out.append({
            "kind": tag,           # replace / delete / insert
            "old": o,
            "new": n,
            "context": context,
            "tags": tags,
            "critical": bool(set(tags) & CRITICAL_TAGS),
        })
    return out


# ---------------------------------------------------------------- 比对主逻辑


def compare_text(text_a: str, text_b: str) -> dict:
    a, b = split_clauses(text_a), split_clauses(text_b)
    pairs, added, removed = pair_clauses(a, b)

    changes: list[dict] = []

    for i, j, how in pairs:
        ca, cb = a[i], b[j]
        # 正文（不含标题）比较：标题里的「第4条→第5条」是重新编号，不是内容改动，
        # 混在一起会让每一条都被判成「修改」（实测踩过）
        body_a = "\n".join(ca["body"])
        body_b = "\n".join(cb["body"])
        renumbered = ca["id"] != cb["id"]
        if body_a == body_b:
            if renumbered:
                changes.append({
                    "clause": cb["id"], "clause_old": ca["id"],
                    "title": cb["title"][:80], "type": "仅编号变更", "matched_by": how,
                    "critical": False, "tags": ["编号"],
                    "diffs": [{"kind": "renumber", "old": ca["id"], "new": cb["id"],
                               "context": "", "tags": ["编号"], "critical": False}],
                })
            continue
        diffs = char_diff(body_a, body_b)
        if not diffs:
            continue
        if renumbered:
            diffs.insert(0, {"kind": "renumber", "old": ca["id"], "new": cb["id"],
                             "context": "", "tags": ["编号"], "critical": False})
        fields = field_diff(body_a, body_b)
        changes.append({
            "clause": cb["id"],
            "clause_old": ca["id"],
            "title": cb["title"][:80],
            "type": "修改",
            "matched_by": how,
            "critical": bool(fields) or any(d["critical"] for d in diffs),
            "tags": sorted({t for d in diffs for t in d["tags"]} | {f["field"] for f in fields}),
            "fields": fields,
            "diffs": diffs,
        })

    for j in added:
        changes.append({
            "clause": b[j]["id"], "clause_old": "", "title": b[j]["title"][:80],
            "type": "新增", "matched_by": "—", "critical": True,
            "tags": tag_change("", b[j]["text"]),
            "diffs": [{"kind": "insert", "old": "", "new": b[j]["text"][:400],
                       "context": "", "tags": tag_change("", b[j]["text"]),
                       "critical": True}],
        })

    for i in removed:
        changes.append({
            "clause": "", "clause_old": a[i]["id"], "title": a[i]["title"][:80],
            "type": "删除", "matched_by": "—", "critical": True,
            "tags": tag_change(a[i]["text"], ""),
            "diffs": [{"kind": "delete", "old": a[i]["text"][:400], "new": "",
                       "context": "", "tags": tag_change(a[i]["text"], ""),
                       "critical": True}],
        })

    # 关键变更排前面，其次按条款顺序
    changes.sort(key=lambda c: (not c["critical"], c["clause"] or "zzz"))

    return {
        "summary": {
            "clauses_a": len(a),
            "clauses_b": len(b),
            "changed": len(changes),
            "critical": sum(1 for c in changes if c["critical"]),
            "added": len(added),
            "removed": len(removed),
            "modified": len(changes) - len(added) - len(removed),
        },
        "changes": changes,
    }


def render(result: dict, title: str = "合同比对") -> str:
    s = result["summary"]
    lines = [
        f"=== {title} ===",
        f"条款数：A {s['clauses_a']} 条 / B {s['clauses_b']} 条",
        f"改动：{s['changed']} 处（关键 {s['critical']}）"
        f"  修改 {s['modified']} · 新增 {s['added']} · 删除 {s['removed']}",
        "",
    ]
    if not result["changes"]:
        lines.append("两份文件在整个条款层面没有差异。")
        return "\n".join(lines)

    for n, c in enumerate(result["changes"], 1):
        flag = "【关键】" if c["critical"] else "【一般】"
        clause = c["clause"] or c["clause_old"]
        name = c["title"]
        if clause and name.startswith(clause):
            name = name[len(clause):].strip()
        lines.append(f"{n}. {flag} {clause} {name}  ({c['type']}，配对于{c['matched_by']})")
        if c["tags"]:
            lines.append(f"     涉及：{'、'.join(c['tags'])}")
        # 字段级对比排在最前：法务看的是「日期从 X 改成 Y」
        for f in c.get("fields", []):
            gone = "、".join(f["removed"]) or "—"
            came = "、".join(f["added"]) or "—"
            lines.append(f"     [{f['field']}] {gone}  →  {came}")
        for d in c["diffs"][:6]:
            if d["kind"] == "renumber":
                lines.append(f"     编号：{d['old']} → {d['new']}（内容未变）")
            elif d["kind"] == "replace":
                lines.append(f"     旧：{d['old'][:120]}")
                lines.append(f"     新：{d['new'][:120]}")
            elif d["kind"] == "delete":
                lines.append(f"     删除：{d['old'][:120]}")
            else:
                lines.append(f"     插入：{d['new'][:120]}")
            if d["context"]:
                lines.append(f"     上下文：…{d['context'][:140]}…")
        if len(c["diffs"]) > 6:
            lines.append(f"     （另有 {len(c['diffs']) - 6} 处细改动，见 --json）")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------- 入口


def load(path: Path, kind: str) -> str:
    if kind == "pdf":
        from pdf_to_md import extract
        return extract(path)
    return path.read_text(encoding="utf-8")


def selftest() -> int:
    """自测：造一对合同，验证「能定位到具体字词」和「编号错位也能配上」。"""
    a = """采购框架协议

第1条 合作范围
甲方向乙方采购办公用品，具体品类以附件一为准。

第2条 合同金额
本合同总金额为人民币 120 万元整（含税）。

第3条 付款期限
甲方应于收到乙方发票后 30 个工作日内支付全部款项。

第4条 违约责任
任何一方违约，应向对方支付合同总额 5% 的违约金。

第5条 争议解决
双方争议提交深圳市南山区人民法院管辖。

第6条 保密
双方对本协议内容负保密义务，保密期 3 年。
"""

    b = """采购框架协议

第1条 合作范围
甲方向乙方采购办公用品及电子设备，具体品类以附件一为准。

第2条 合同金额
本合同总金额为人民币 180 万元整（含税）。

第4条 付款期限
甲方应于收到乙方发票后 45 个工作日内支付全部款项。

第5条 违约责任
任何一方违约，应向对方支付合同总额 15% 的违约金，并赔偿实际损失。

第6条 争议解决
双方争议提交深圳市南山区人民法院管辖。

第7条 知识产权
乙方交付的成果知识产权归甲方所有。
"""
    # 注意：B 少第 6 条保密（要判为删除）、多第 7 条（新增）、
    # 付款期限从「第3条」挪到「第4条」（编号错位，靠相似度也要配上）

    r = compare_text(a, b)
    s = r["summary"]
    checks: list[tuple[str, bool, str]] = []

    def ck(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, ok, detail))

    ck("识别出改动", s["changed"] >= 3, f"changed={s['changed']}")
    ck("认出新增条款", s["added"] == 1, f"added={s['added']}")
    ck("认出删除条款", s["removed"] == 1, f"removed={s['removed']}")
    ck("新增被标为关键", any(c["type"] == "新增" and c["critical"] for c in r["changes"]), "")

    # 金额改动必须定位到具体数字，而不是整段
    money = [c for c in r["changes"] if "金额" in c["tags"] and c["type"] == "修改"]
    ck("定位到金额改动", len(money) >= 1, f"{len(money)} 处")
    if money:
        d = money[0]["diffs"][0]
        ck("金额改动只框住数字", "120" in d["old"] and "180" in d["new"] and len(d["old"]) <= 8,
           f"old={d['old']!r} new={d['new']!r}")

    # 期限：编号从第3条挪到第4条，仍要能配上
    term = [c for c in r["changes"] if "期限" in c["tags"]]
    ck("编号错位的条款仍配上", any(c["matched_by"].startswith("相似") for c in term),
       f"{[c['matched_by'] for c in term]}")

    # 责任条款：5% → 15% + 加一句
    duty = [c for c in r["changes"] if "责任" in c["tags"] and c["type"] == "修改"]
    ck("责任条款改动被标为关键", all(c["critical"] for c in duty) and len(duty) >= 1, f"{len(duty)} 处")

    # 没改的条款不能报成改动（第1条只加「及电子设备」一处）
    first = [c for c in r["changes"] if c["clause_old"] == "第1条" or c["clause"] == "第1条"]
    ck("未改动的条款不误报", len(first) == 1 and len(first[0]["diffs"]) == 1,
       f"{len(first)} 条 / {[len(c['diffs']) for c in first]} 处")

    print(render(r, "自测：采购框架协议 A vs B"))
    print("=" * 60)
    bad = 0
    for name, ok, detail in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
        bad += 0 if ok else 1
    print("=" * 60)
    print(f"通过 {len(checks) - bad} 项，失败 {bad} 项")
    return 0 if bad == 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="合同版本比对（条款级 diff）")
    ap.add_argument("a", nargs="?", help="旧版 PDF")
    ap.add_argument("b", nargs="?", help="新版 PDF")
    ap.add_argument("--md", help="旧版 markdown（替代 PDF）")
    ap.add_argument("--md2", help="新版 markdown")
    ap.add_argument("--json", dest="json_out", help="把结构化结果写到这个路径")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    if args.md and args.md2:
        text_a, text_b = load(Path(args.md), "md"), load(Path(args.md2), "md")
        title = f"{Path(args.md).name} vs {Path(args.md2).name}"
    elif args.a and args.b:
        text_a, text_b = load(Path(args.a), "pdf"), load(Path(args.b), "pdf")
        title = f"{Path(args.a).name} vs {Path(args.b).name}"
    else:
        ap.error("要么给两个 PDF，要么 --md 与 --md2，要么 --selftest")

    result = compare_text(text_a, text_b)
    print(render(result, title))
    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"结构化结果：{args.json_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
