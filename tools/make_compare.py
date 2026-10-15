# -*- coding: utf-8 -*-
"""把输出图拼成便于目视检查的对比图"""
import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "test" / "out"
CMP = ROOT / "test" / "cmp"
CMP.mkdir(parents=True, exist_ok=True)


def load(name):
    return Image.open(OUT / name).convert("L")


def fit_h(img, h):
    w = int(img.width * h / img.height)
    return img.resize((w, h), Image.LANCZOS)


def side_by_side(names, out_name, h=1000):
    imgs = [fit_h(load(n), h) for n in names]
    gap = 12
    W = sum(i.width for i in imgs) + gap * (len(imgs) - 1)
    canvas = Image.new("L", (W, h), 128)
    x = 0
    for i in imgs:
        canvas.paste(i, (x, 0))
        x += i.width + gap
    canvas.save(CMP / out_name)
    print(out_name, canvas.size)


def crop_grid(specs, out_name, box_frac, h=420):
    """specs: [(file, label)] 按给定相对框裁剪并横排"""
    tiles = []
    for name, _label in specs:
        im = load(name)
        x0, y0, x1, y1 = (int(box_frac[0] * im.width), int(box_frac[1] * im.height),
                          int(box_frac[2] * im.width), int(box_frac[3] * im.height))
        c = im.crop((x0, y0, x1, y1))
        c = c.resize((int(c.width * h / c.height), h), Image.LANCZOS)
        tiles.append(c)
    gap = 10
    W = sum(t.width for t in tiles) + gap * (len(tiles) - 1)
    canvas = Image.new("L", (W, h), 128)
    x = 0
    for t in tiles:
        canvas.paste(t, (x, 0))
        x += t.width + gap
    canvas.save(CMP / out_name)
    print(out_name, canvas.size)


# 1) 整页对比：同内容不同引擎
side_by_side(["sample_mono_p002.png", "sample_colorlaser_p002.png",
              "sample_colorink_p002.png"], "cmp_p2_full.png", h=1100)

# 2) 灰度阶梯区域（第 1 页上部）
crop_grid([("sample_mono_p001.png", "mono")], "cmp_mono_ramp.png",
          (0.06, 0.16, 0.97, 0.30), h=380)

# 3) 彩色文字 + 色块区域（第 2 页上部）三引擎对比
crop_grid([("sample_mono_p002.png", "mono"),
           ("sample_colorlaser_p002.png", "claser"),
           ("sample_colorink_p002.png", "cink")], "cmp_color_top.png",
          (0.08, 0.11, 0.95, 0.40), h=520)

# 4) 深色块区域
crop_grid([("sample_mono_p002.png", "mono"),
           ("sample_colorlaser_p002.png", "claser"),
           ("sample_colorink_p002.png", "cink")], "cmp_dark.png",
          (0.08, 0.66, 0.95, 0.76), h=340)

# 5) 密排小字（第 3 页）锐度对比
crop_grid([("sample_mono_p003.png", "mono"),
           ("sample_colorlaser_p003.png", "claser")], "cmp_smalltext.png",
          (0.08, 0.36, 0.60, 0.47), h=340)
