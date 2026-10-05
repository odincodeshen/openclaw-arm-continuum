#!/usr/bin/env python3
"""Regenerate the gold image set (verify/gold/images/*.png + .txt truth).

Run on a host with DejaVu Sans and Noto Sans CJK (and Pillow); the sandbox
has no CJK fonts, so the images are committed. Everything shown is made up.

    python3 verify/gold/make_images.py
"""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

OUT = Path(__file__).resolve().parent / "images"
LATIN = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
CJK = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
TC, SC = 3, 2  # face indexes in the Noto CJK collection


def font(path: str, size: int, index: int = 0):
    return ImageFont.truetype(path, size, index=index)


def draw(lines: list[tuple[str, object]], width: int, line_height: int, *, fg=(0, 0, 0), bg=(255, 255, 255)):
    image = Image.new("RGB", (width, 50 + line_height * len(lines)), bg)
    pen = ImageDraw.Draw(image)
    for i, (text, face) in enumerate(lines):
        pen.text((36, 25 + line_height * i), text, fill=fg, font=face)
    return image


def save(name: str, image, lines: list[tuple[str, object]]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    image.save(OUT / f"{name}.png")
    (OUT / f"{name}.txt").write_text("\n".join(text for text, _ in lines) + "\n", encoding="utf-8")


def main() -> None:
    receipt = [("Sample receipt 4417", font(LATIN, 40)),
               ("繁體：測試發票 NT$ 280", font(CJK, 40, TC)),
               ("简体：电子发票 ¥612.5", font(CJK, 40, SC))]
    clean = draw(receipt, 900, 70)
    save("receipt_clean", clean, receipt)
    save("receipt_rotated", clean.rotate(4, expand=True, fillcolor=(255, 255, 255)), receipt)
    save("receipt_blurred", clean.filter(ImageFilter.GaussianBlur(1.2)), receipt)
    save("receipt_low_contrast", draw(receipt, 900, 70, fg=(150, 150, 150), bg=(225, 225, 225)), receipt)

    small = font(LATIN, 18)
    english = [(text, small) for text in [
        "Maintenance notice for building B, server room 2.",
        "The room closes on 14 November 2026 from 08:30 to 17:00 for cooling work.",
        "Racks R4 to R9 lose power between 09:15 and 11:45.",
        "Racks R1 to R3 stay on the backup feed during the work.",
        "Contact the facilities desk on extension 4471, ticket FM-2026-0388.",
        "Approved budget: GBP 18,640.50 excluding VAT.",
        "Move test jobs off nodes gpu-07 and gpu-12 before 08:00.",
    ]]
    save("small_print_en", draw(english, 1000, 30), english)

    tc = font(CJK, 30, TC)
    traditional = [(text, tc) for text in [
        "社區活動中心使用公告",
        "自十一月三日起，二樓多功能教室開放預約，",
        "每週一至週五上午九點至晚上九點。",
        "每次使用最多三小時，押金新臺幣一千五百元，",
        "活動結束後須恢復桌椅原狀並關閉冷氣。",
        "逾時十五分鐘以上，將酌收清潔費三百元。",
    ]]
    save("notice_zh_tw", draw(traditional, 1100, 52), traditional)

    sc = font(CJK, 30, SC)
    table = [(text, sc) for text in [
        "办公用品采购清单",
        "项目 | 数量 | 单价",
        "打印纸 | 20 | 23.50",
        "订书机 | 4 | 18.00",
        "白板笔 | 36 | 2.80",
        "合计 | 60 | 642.80",
    ]]
    save("table_zh_cn", draw(table, 800, 52), table)
    for path in sorted(OUT.glob("*.png")):
        print(path.name)


if __name__ == "__main__":
    main()
