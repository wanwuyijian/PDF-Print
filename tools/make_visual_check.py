# -*- coding: utf-8 -*-
"""观感验收：把不同参数下的扫描结果拼成对比图，便于肉眼检查

产出 test/cmp/*.png
"""
import sys
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from app import effects, engine                       # noqa: E402
from app.config import (LayoutParams, OutputParams, PrintParams,  # noqa: E402
                        RenderJob, ScanParams)

OUT = ROOT / "test" / "cmp"
OUT.mkdir(parents=True, exist_ok=True)
SAMPLE = ROOT / "test" / "sample.pdf"


def render(pages, job, seed=20261109, max_side=760):
    """按 job 逐页出图，返回 [(page_no, ndarray)]"""
    res = []
    with engine.open_pdf(SAMPLE) as doc:
        total = doc.page_count
        for pidx in pages:
            if pidx >= total:
                continue
            page = doc[pidx]
            sheet_mm = engine.page_size_mm(page)
            dpi = job.output_params.render_dpi
            rgb = engine.render_page_rgb(page, dpi)
            sheet_w = int(round(sheet_mm[0] / 25.4 * dpi))
            sheet_h = int(round(sheet_mm[1] / 25.4 * dpi))
            sheet = engine.compose_sheet(rgb, sheet_w, sheet_h, 0, "fit")
            rng = np.random.default_rng(
                effects.page_seed(seed, pidx, job.scan_params.page_variation))
            skew = effects.resolve_skew(job.scan_params, seed, pidx)
            arr = effects.simulate(sheet, job.print_params, job.scan_params, rng,
                                   skew_deg=skew)
            im = Image.fromarray(arr, mode="RGB" if arr.ndim == 3 else "L")
            im.thumbnail((max_side, max_side), Image.LANCZOS)
            res.append((pidx + 1, np.asarray(im.convert("RGB"))))
    return res


def strip(rows, labels, path, gap=10):
    """rows: list of list of (page, arr)；labels: 每行标题"""
    pad = 8
    w = max(sum(a.shape[1] for _, a in row) + gap * (len(row) - 1) for row in rows) + pad * 2
    h = sum(max(a.shape[0] for _, a in row) for row in rows) + gap * (len(rows) - 1) + pad * 2 + 18 * len(rows)
    canvas = Image.new("RGB", (w, h), (245, 245, 245))
    y = pad
    from PIL import ImageDraw
    d = ImageDraw.Draw(canvas)
    for label, row in zip(labels, rows):
        d.text((pad, y + 2), label, fill=(20, 20, 20))
        y += 18
        x = pad
        for _, a in row:
            canvas.paste(Image.fromarray(a), (x, y))
            x += a.shape[1] + gap
        y += max(a.shape[0] for _, a in row) + gap
    canvas.save(path)
    print("  ->", path.relative_to(ROOT), canvas.size)


def job(**kw):
    pp = PrintParams(**{k: v for k, v in kw.items() if k in PrintParams.__dataclass_fields__})
    sp = ScanParams(**{k: v for k, v in kw.items() if k in ScanParams.__dataclass_fields__})
    return RenderJob(pp, sp, LayoutParams(), OutputParams(render_dpi=150))


def main():
    pages = [0, 1, 2]

    print("[1] 页面间效果：统一 / 仅倾斜 / 逐页随机")
    rows, labels = [], []
    for mode, name in (("uniform", "整份统一（默认）"), ("jitter", "仅倾斜微抖"),
                       ("random", "逐页随机")):
        rows.append(render(pages, job(page_variation=mode, skew_random=0.9)))
        labels.append(f"{name}  page_variation={mode}")
    strip(rows, labels, OUT / "cmp_page_variation.png")

    print("[2] 纸张类型 / 底灰")
    rows, labels = [], []
    for pt, fog, name in (("plain", 0.10, "普通纸 fog=0.10（默认）"),
                          ("coated", 0.10, "铜版纸 fog=0.10"),
                          ("recycled", 0.10, "再生纸 fog=0.10"),
                          ("plain", 0.45, "普通纸 fog=0.45（重底灰）")):
        rows.append(render([0], job(paper_type=pt, fog=fog)))
        labels.append(name)
    strip(rows, labels, OUT / "cmp_paper.png")

    print("[3] 灰尘 / 纵向条纹")
    rows, labels = [], []
    for dust, streak, name in ((0.0, 0.0, "干净 dust=0 streak=0"),
                               (0.35, 0.0, "灰尘 dust=0.35"),
                               (0.0, 0.6, "条纹 streak=0.6"),
                               (0.35, 0.6, "两者 dust=0.35 streak=0.6")):
        rows.append(render([0], job(dust=dust, streak=streak, noise=0.35)))
        labels.append(name)
    strip(rows, labels, OUT / "cmp_dust_streak.png")

    print("[4] 彩色：纸张类型 + 条纹")
    rows, labels = [], []
    for pt, streak, name in (("plain", 0.0, "彩色·普通纸"),
                             ("recycled", 0.0, "彩色·再生纸"),
                             ("plain", 0.5, "彩色·条纹 0.5")):
        rows.append(render([1], job(mode="color_laser", out_mode="color",
                                    paper_type=pt, streak=streak,
                                    paper_warmth=0.45, color_cast=0.3)))
        labels.append(name)
    strip(rows, labels, OUT / "cmp_color.png")


main()
