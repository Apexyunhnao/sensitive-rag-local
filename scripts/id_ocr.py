"""身份证图片 OCR 提取：本地引擎 + 规则校验（全程不出网）。

流程：生成模拟身份证图 → RapidOCR 识别 → 提取字段 → 校验位验证
"""
from __future__ import annotations

import re
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT = Path("E:/Projects/sensitive-rag-demo/out")
FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\simsun.ttc",
]

WEIGHTS = [7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2]
CHECK = "10X98765432"
ID_RE = re.compile(r"(?<!\d)(\d{17}[\dXx])(?!\d)")


def make_id_card(path: Path, name="林嘉欣", idnum="44030419900307123X") -> None:
    """画一张模拟身份证（横版，85.6×54mm 比例）。"""
    W, H = 1000, 631
    img = Image.new("RGB", (W, H), (238, 240, 245))
    d = ImageDraw.Draw(img)

    font_path = next((f for f in FONT_CANDIDATES if Path(f).exists()), None)
    f_big = ImageFont.truetype(font_path, 42) if font_path else ImageFont.load_default()
    f_mid = ImageFont.truetype(font_path, 32) if font_path else ImageFont.load_default()

    d.rectangle([20, 20, W - 20, H - 20], outline=(180, 185, 200), width=3)
    d.text((60, 50), "中华人民共和国", font=f_big, fill=(30, 30, 40))
    d.text((60, 100), "居民身份证", font=f_mid, fill=(30, 30, 40))

    rows = [
        ("姓    名", name),
        ("性    别", "女        民 族  汉"),
        ("出    生", "1990 年 3 月 7 日"),
        ("住    址", "广东省深圳市南山区科技园路 88 号 3 栋 1201"),
        ("公民身份号码", idnum),
    ]
    y = 200
    for label, value in rows:
        d.text((60, y), label, font=f_mid, fill=(60, 60, 70))
        d.text((260, y), value, font=f_mid, fill=(20, 20, 25))
        y += 70

    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, quality=95)
    print(f"已生成模拟身份证图: {path} ({W}x{H})")


def check_digit(first17: str) -> str:
    return CHECK[sum(int(c) * w for c, w in zip(first17, WEIGHTS)) % 11]


def validate(num: str) -> tuple[bool, str]:
    num = num.upper()
    if len(num) != 18:
        return False, f"长度 {len(num)}"
    if not num[:17].isdigit():
        return False, "前 17 位含非数字"
    if check_digit(num[:17]) != num[17]:
        return False, "校验位不匹配"
    return True, "通过"


def main() -> None:
    img_path = OUT / "id_card_sample.png"
    make_id_card(img_path)

    from rapidocr_onnxruntime import RapidOCR

    print("\n初始化 OCR 引擎…")
    t0 = time.time()
    ocr = RapidOCR()
    print(f"引擎就绪 {(time.time()-t0)*1000:.0f} ms")

    t1 = time.time()
    result, _ = ocr(str(img_path))
    ocr_ms = (time.time() - t1) * 1000

    if not result:
        print("未识别到文字")
        return

    print(f"\n识别到 {len(result)} 段文字，耗时 {ocr_ms:.0f} ms")
    print("-" * 70)
    lines = []
    for box, text, score in result:
        lines.append(text)
        print(f"  conf={score:.2f}  {text}")

    full = "\n".join(lines)
    print("\n=== 结构化提取 ===")
    m = ID_RE.search(full.replace(" ", ""))
    if m:
        num = m.group(1).upper()
        ok, why = validate(num)
        print(f"身份证号: {num}")
        print(f"校验: {'✓ ' + why if ok else '✗ ' + why}")
        if ok:
            print(f"地区码: {num[:2]}")
            print(f"出生: {num[6:10]}-{num[10:12]}-{num[12:14]}")
            print(f"性别: {'男' if int(num[16]) % 2 else '女'}")
    else:
        print("未找到 18 位身份证号（可能被空格/换行断开）")

    m2 = re.search(r"[\u4e00-\u9fa5]{2,4}", full.split("\n")[0] if lines else "")
    print(f"姓名候选: {m2.group(0) if m2 else '未识别'}")
    print(f"\n全程耗时: {(time.time()-t0)*1000:.0f} ms，零网络请求")


if __name__ == "__main__":
    main()
