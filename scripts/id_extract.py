"""身份证号提取：规则引擎方案（正则 + 校验位 + 地址码）。

对比要点：
- 不调任何模型，毫秒级、零外发、结果确定
- 校验位算法能滤掉随机数字（通过率约 1/11）
- 大模型在这类任务上会改数字、无法自证对错

用法：python scripts/id_extract.py
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass

# 前两位：省级行政区代码
PROVINCE = {
    "11": "北京", "12": "天津", "13": "河北", "14": "山西", "15": "内蒙古",
    "21": "辽宁", "22": "吉林", "23": "黑龙江", "31": "上海", "32": "江苏",
    "33": "浙江", "34": "安徽", "35": "福建", "36": "江西", "37": "山东",
    "41": "河南", "42": "湖北", "43": "湖南", "44": "广东", "45": "广西",
    "46": "海南", "50": "重庆", "51": "四川", "52": "贵州", "53": "云南",
    "54": "西藏", "61": "陕西", "62": "甘肃", "63": "青海", "64": "宁夏",
    "65": "新疆", "71": "台湾", "81": "香港", "82": "澳门", "91": "国外",
}

WEIGHTS = [7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2]
CHECK = "10X98765432"

# 宽松抓取：18 位（末位可为 X）或 15 位旧号，允许中间有空格/短横
CANDIDATE = re.compile(r"(?<!\d)(\d{6}[\s\-]?(\d{8})[\s\-]?(\d{3}[\dXx]))(?!\d)")


@dataclass
class Hit:
    raw: str
    normalized: str
    valid: bool
    reason: str
    province: str = ""
    birth: str = ""
    gender: str = ""


def check_digit(first17: str) -> str:
    """按 GB 11643-1999 算第 18 位校验码。"""
    s = sum(int(c) * w for c, w in zip(first17, WEIGHTS))
    return CHECK[s % 11]


def validate(num: str) -> tuple[bool, str]:
    if len(num) == 15:                       # 15 位旧号，无校验位
        return True, "15位旧号（无校验位）"
    if len(num) != 18:
        return False, f"长度异常 {len(num)}"
    if not num[:17].isdigit():
        return False, "前17位含非数字"
    if check_digit(num[:17]) != num[17].upper():
        return False, "校验位不匹配（疑似误识别或无效号）"
    if num[:2] not in PROVINCE:
        return False, f"地址码 {num[:2]} 不存在"
    return True, "通过"


def extract(text: str) -> list[Hit]:
    out: list[Hit] = []
    for m in CANDIDATE.finditer(text):
        raw = m.group(1)
        num = re.sub(r"[\s\-]", "", raw).upper()
        ok, why = validate(num)
        province = PROVINCE.get(num[:2], "")
        birth = f"{num[6:10]}-{num[10:12]}-{num[12:14]}" if len(num) >= 14 else ""
        gender = "男" if len(num) == 18 and int(num[16]) % 2 else "女" if len(num) == 18 else ""
        out.append(Hit(raw, num, ok, why, province, birth, gender))
    return out


SAMPLE = """
员工入职材料（模拟）：
姓名：林嘉欣   身份证：440304 19900307 123X      ← 有效号
姓名：黄志强   身份证：110105-19880915-5672      ← 有效号（带分隔符）
姓名：吴嘉敏   身份证：310101198512034564         ← 有效号
姓名：张三     身份证：440304199003071235         ← 末位错，校验位不通过
编号：A123456789012345678                        ← 18位但不是身份证，校验失败
电话：13800138000                                ← 11位数字，不匹配
"""


def main() -> None:
    t0 = time.time()
    hits = extract(SAMPLE)
    elapsed = (time.time() - t0) * 1000

    print(f"扫描 {len(SAMPLE)} 字符，耗时 {elapsed:.1f} ms\n")
    print(f"{'原始':<26}{'归一化':<20}{'结果':<8}{'说明'}")
    print("-" * 90)
    for h in hits:
        flag = "✓ 有效" if h.valid else "✗ 无效"
        print(f"{h.raw:<26}{h.normalized:<20}{flag:<8}{h.reason}")

    valid = [h for h in hits if h.valid]
    print(f"\n候选 {len(hits)} 个，有效 {len(valid)} 个，拦截误识别 {len(hits)-len(valid)} 个")
    for h in valid:
        print(f"  {h.normalized} | {h.province} | 生日 {h.birth} | {h.gender}")


if __name__ == "__main__":
    main()
