# -*- coding: utf-8 -*-
"""
生成测试用 PDF（含黑白文字 / 灰度色块 / 彩色图文 / 照片式渐变），
用于验证打印扫描效果是否符合预期。
"""
import sys
from pathlib import Path

import pymupdf as fitz

OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("test/sample.pdf")
OUT.parent.mkdir(parents=True, exist_ok=True)

doc = fitz.open()

# ---------------- 第 1 页：中文/英文正文 + 灰度色块 ----------------
p = doc.new_page(width=595, height=842)  # A4 pt
p.insert_text((60, 80), "打印扫描效果测试文档", fontname="china-s", fontsize=22)
p.insert_text((60, 110), "Print / Scan Simulation Test", fontname="helv", fontsize=12)

body = [
    "本页用于验证黑白激光打印机效果：正文文字应当边缘锐利、笔画饱满，",
    "白底保持干净，灰阶过渡区域应当出现轻微网点（抖动的半色调纹理）。",
    "",
    "The quick brown fox jumps over the lazy dog. 0123456789",
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ abcdefghijklmnopqrstuvwxyz",
]
y = 150
for line in body:
    if line:
        p.insert_text((60, y), line, fontname="china-s" if line[0] > "\u4e00" else "helv",
                      fontsize=10.5)
    y += 20

# 灰度色块
p.insert_text((60, y + 10), "灰度阶梯 / Gray ramp:", fontname="china-s", fontsize=10)
y += 30
for i in range(11):
    v = i / 10
    g = int(255 * (1 - v))
    col = (g / 255, g / 255, g / 255)
    rect = fitz.Rect(60 + i * 44, y, 60 + i * 44 + 40, y + 46)
    p.draw_rect(rect, color=None, fill=col)
p.insert_text((60, y + 70), "预期：中间调可见网点，纯黑区最黑，纯白区不脏。",
              fontname="china-s", fontsize=9)

# 细线测试（考验锐度）
p.insert_text((60, y + 100), "细线 / Hairlines:", fontname="china-s", fontsize=10)
for i, w in enumerate((0.3, 0.5, 0.75, 1.0, 1.5, 2.0)):
    yy = y + 115 + i * 9
    p.draw_line((60, yy), (520, yy), color=(0, 0, 0), width=w)

# ---------------- 第 2 页：彩色图文 ----------------
p2 = doc.new_page(width=595, height=842)
p2.insert_text((60, 80), "彩色打印效果测试", fontname="china-s", fontsize=22)

colors = [
    ("红 Red", (1, 0, 0)), ("绿 Green", (0, 0.55, 0)), ("蓝 Blue", (0, 0, 1)),
    ("青 Cyan", (0, 0.7, 0.9)), ("品红 Magenta", (1, 0, 0.7)), ("黄 Yellow", (1, 0.85, 0)),
    ("橙 Orange", (1, 0.5, 0)), ("紫 Purple", (0.5, 0, 0.8)),
]
yy = 120
for name, c in colors:
    p2.draw_rect(fitz.Rect(60, yy, 150, yy + 34), color=None, fill=c)
    p2.insert_text((165, yy + 22), f"{name}   —— 灰度化后深浅各不相同",
                   fontname="china-s", fontsize=10, color=(0, 0, 0))
    yy += 44

p2.insert_text((60, yy + 20), "彩色文字（转为灰度扫描件后会变成不同深度的灰）:",
               fontname="china-s", fontsize=10)
yy += 45
p2.insert_text((60, yy), "红色文字红色文字", fontname="china-s", fontsize=16, color=(1, 0, 0))
p2.insert_text((200, yy), "蓝色文字蓝色文字", fontname="china-s", fontsize=16, color=(0, 0, 1))
p2.insert_text((340, yy), "绿色文字绿色文字", fontname="china-s", fontsize=16, color=(0, 0.5, 0))

# 彩色渐变条（考验总墨量限制与暗部发闷）
yy += 40
p2.insert_text((60, yy), "RGB 渐变（考验 TAC 总墨量限制与暗部表现）:",
               fontname="china-s", fontsize=10)
yy += 12
steps = 24
for i in range(steps):
    t = i / (steps - 1)
    r = max(0.0, 1 - abs(3 * t - 0) % 1)
    col = (min(1, 1.6 * t), 0.35 + 0.3 * (1 - t), max(0.0, 1 - 1.6 * t))
    p2.draw_rect(fitz.Rect(60 + i * 19.5, yy, 60 + i * 19.5 + 19, yy + 60),
                 color=None, fill=col)

# 深色照片式色块（验证合成黑）
yy += 80
p2.insert_text((60, yy), "深色块（彩色打印的黑是 CMY 合成黑，灰度化后偏灰）:",
               fontname="china-s", fontsize=10)
for i, (r, g, b) in enumerate([(0.05, 0.05, 0.05), (0.1, 0.1, 0.12),
                               (0.15, 0.1, 0.1), (0.1, 0.12, 0.1)]):
    p2.draw_rect(fitz.Rect(60 + i * 120, yy + 10, 60 + i * 120 + 110, yy + 80),
                 color=None, fill=(r, g, b))

# ---------------- 第 3 页：整页深色图（压力测试）----------------
p3 = doc.new_page(width=595, height=842)
p3.insert_text((60, 60), "压力测试页：大面积深色 + 密排小字", fontname="china-s", fontsize=14)
p3.draw_rect(fitz.Rect(60, 80, 535, 300), color=None, fill=(0.06, 0.06, 0.09))
for i in range(26):
    p3.insert_text((60, 320 + i * 14),
                   f"第 {i + 1:02d} 行：密集小字测试 0123456789 ABCDEFG abcdefg 中文小字测试",
                   fontname="china-s", fontsize=7.5, color=(1, 0, 0) if i % 3 == 0 else (0, 0, 0))

doc.save(str(OUT))
doc.close()
print(f"OK -> {OUT.resolve()}")
