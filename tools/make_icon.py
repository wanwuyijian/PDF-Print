# -*- coding: utf-8 -*-
"""生成程序图标 assets/icon.ico（多尺寸），无需外部素材"""
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "assets"
OUT.mkdir(parents=True, exist_ok=True)

S = 256
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)

# 圆角底：深蓝灰
d.rounded_rectangle((8, 8, S - 8, S - 8), radius=44, fill=(34, 51, 71, 255))

# 纸张（白）
d.rounded_rectangle((62, 40, 194, 176), radius=10, fill=(246, 246, 243, 255))
# 纸上的“文字行” —— 其中一行用灰度网点表示扫描效果
line_colors = [(70, 70, 70, 255), (70, 70, 70, 255), (70, 70, 70, 255)]
y = 70
for i, c in enumerate(line_colors):
    d.rounded_rectangle((80, y, 176 - i * 22, y + 11), radius=5, fill=c)
    y += 26

# 网点行（半色调效果）
import random
random.seed(7)
for gx in range(80, 177, 9):
    for gy in range(y, y + 14, 7):
        if random.random() < 0.62:
            d.ellipse((gx, gy, gx + 5, gy + 5), fill=(90, 90, 90, 255))

# 扫描光束（青绿斜线，象征扫描）
for i in range(4):
    x = 40 + i * 14
    d.line((x, 210, x + 46, 186), fill=(64, 214, 180, 210), width=6)

# 打印机主体（下方）
d.rounded_rectangle((46, 168, 210, 232), radius=18, fill=(58, 96, 138, 255))
# 出纸口
d.rounded_rectangle((78, 158, 178, 176), radius=6, fill=(232, 236, 240, 255))
# 指示灯
d.ellipse((186, 184, 200, 198), fill=(64, 214, 180, 255))

img.save(OUT / "icon.png")
img.save(OUT / "icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48),
                                  (64, 64), (128, 128), (256, 256)])
print("OK ->", OUT / "icon.ico")
