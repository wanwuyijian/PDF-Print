# -*- coding: utf-8 -*-
"""生成一张「原图 → 三种打印引擎效果」的展示对比图"""
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from app import engine, logsetup                        # noqa: E402
from app.config import (LayoutParams, OutputParams,      # noqa: E402
                        PrintParams, RenderJob, ScanParams)

logsetup.init_logging()
SRC = ROOT / "test" / "sample.pdf"
OUT = ROOT / "test" / "cmp" / "效果对比总览.png"

PAGE = 1            # 第 2 页（彩色内容）
DPI = 200
MAX_W = 560         # 每格显示宽度


def render(mode: str | None):
    job = RenderJob(
        print_params=PrintParams(mode=mode or "mono_laser"),
        scan_params=ScanParams(),
        layout_params=LayoutParams(paper="A4", margin_mm=8),
        output_params=OutputParams(render_dpi=DPI, out_dpi=DPI))

    if mode is None:                                    # 原图：不做任何效果
        doc = engine.open_pdf(SRC)
        try:
            page = doc.load_page(PAGE)
            sw = int(page.rect.width / 72 * DPI)
            sh = int(page.rect.height / 72 * DPI)
            m = int(8 / 25.4 * DPI)
            content = engine.render_page_rgb(page, DPI)
            sheet = engine.compose_sheet(content, sw, sh, m, "fit")
            return (np.clip(sheet @ np.array([0.299, 0.587, 0.114], np.float32) * 255,
                            0, 255).astype(np.uint8))
        finally:
            doc.close()

    s = engine.PreviewSession(SRC, job, page_index=PAGE, max_side=10 ** 6)
    return s.rescan(job.scan_params)


def fit(a, w):
    im = Image.fromarray(a, "L")
    h = int(im.height * w / im.width)
    return im.resize((w, h), Image.LANCZOS)


panels = [("原始 PDF（电子文档）", render(None)),
          ("黑白激光打印 → 扫描", render("mono_laser")),
          ("彩色激光打印 → 扫描", render("color_laser")),
          ("彩色喷墨打印 → 扫描", render("color_inkjet"))]

imgs = [fit(a, MAX_W) for _, a in panels]
H = max(i.height for i in imgs)
GAP, TOP = 16, 44
label_h = TOP + 8
W = GAP + len(imgs) * (MAX_W + GAP)

canvas = Image.new("L", (W, H + label_h + GAP), 255)
d = ImageDraw.Draw(canvas)

try:
    font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 22)
    font_s = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 16)
except Exception:
    font = font_s = ImageFont.load_default()

x = GAP
for (title, _), im in zip(panels, imgs):
    canvas.paste(im, (x, label_h))
    d.rectangle([x - 1, label_h - 1, x + im.width, label_h + im.height], outline=150)
    tw = d.textlength(title, font=font_s)
    d.text((x + (im.width - tw) / 2, 12), title, fill=0, font=font_s)
    x += MAX_W + GAP

d.text((GAP, 12), "", fill=0, font=font_s)
canvas.save(OUT)
print("OK ->", OUT, canvas.size)
