# -*- coding: utf-8 -*-
"""
彩色扫描端到端测试（v1.0.4 新增）

v1.0.3 及以前：无论选哪种打印引擎，输出永远是灰度 ——
彩色信息在「打印→扫描」的边界被 print_effect_grayscale 的 `refl @ REC601` 丢掉了。

本测试直接检查**写出来的文件**：像素是不是真的带彩度。
"""
import sys
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from app import engine, logsetup                        # noqa: E402
from app.config import (LayoutParams, OutputParams,      # noqa: E402
                        PrintParams, RenderJob, ScanParams)

logsetup.init_logging()
SRC = ROOT / "test" / "color_sample.pdf"
if not SRC.exists():
    SRC = ROOT / "test" / "sample.pdf"

FAILED: list[str] = []


def check(name, cond, detail=""):
    detail = "" if detail is None else str(detail)
    print(f"  {'✓' if cond else '✗'} {name}{('  ' + detail) if detail else ''}")
    if not cond:
        FAILED.append(name)


def job(print_mode="color_laser", out_mode="gray", fmt="png", out_dir="", **scan_kw):
    sp = ScanParams(out_mode=out_mode, jpeg_quality=92, **scan_kw)
    sp.skew_deg = 0.0                      # 关掉倾斜便于稳定取色块
    op = OutputParams(render_dpi=80, out_dpi=80, out_format=fmt,
                      out_dir=str(out_dir), out_name="", seed=42)
    return RenderJob(PrintParams(mode=print_mode), sp,
                     LayoutParams(paper="A4", margin_mm=8), op)


def chroma_of(path: Path) -> tuple[int, float]:
    """返回 (最大彩度, 平均彩度)。彩度 = max(R,G,B) - min(R,G,B)"""
    im = Image.open(path).convert("RGB")
    a = np.asarray(im, dtype=np.int16)
    ch = a.max(axis=2) - a.min(axis=2)
    return int(ch.max()), float(ch.mean())


tmp = Path(tempfile.mkdtemp(prefix="pdfprint_color_"))

print("=" * 72)
print("① 彩色打印 + 彩色扫描 → 输出必须是彩色")
print("=" * 72)
d1 = tmp / "c1"
r = engine.process_pdf(SRC, job(print_mode="color_laser", out_mode="color",
                                out_dir=d1, paper_warmth=0.30,
                                color_cast=0.18, sat_boost=1.05))
check("处理成功", r.ok, r.error or "")
pngs = sorted(d1.glob("*.png"))
check("产出了图片文件", bool(pngs), f"{[p.name for p in pngs][:3]}")
if pngs:
    mx, avg = chroma_of(pngs[0])
    check("图像是真彩色（最大彩度 > 80）", mx > 80, f"最大彩度 {mx}，平均 {avg:.1f}")
    im = Image.open(pngs[0])
    check("图像模式为 RGB", im.mode == "RGB", f"实际 {im.mode}")

print()
print("=" * 72)
print("② 同样参数下灰度扫描 → 必须仍是灰度（无回归）")
print("=" * 72)
d2 = tmp / "c2"
r = engine.process_pdf(SRC, job(print_mode="color_laser", out_mode="gray", out_dir=d2))
pngs = sorted(d2.glob("*.png"))
if pngs:
    im = Image.open(pngs[0])
    check("图像模式为 L（灰度）", im.mode == "L", f"实际 {im.mode}")
    mx, avg = chroma_of(pngs[0])
    check("灰度图彩度为 0", mx == 0, f"最大彩度 {mx}")

print()
print("=" * 72)
print("③ 二值扫描 → 只有纯黑纯白")
print("=" * 72)
d3 = tmp / "c3"
r = engine.process_pdf(SRC, job(print_mode="mono_laser", out_mode="binary",
                                out_dir=d3, binary_threshold=158))
pngs = sorted(d3.glob("*.png"))
if pngs:
    a = np.asarray(Image.open(pngs[0]).convert("L"))
    uniq = np.unique(a)
    check("只含 0 与 255", set(uniq.tolist()) <= {0, 255}, f"实际取值 {uniq[:6].tolist()}")

print()
print("=" * 72)
print("④ 黑白打印机 + 彩色扫描 → 接近中性灰（物理上就该如此）")
print("=" * 72)
d4 = tmp / "c4"
r = engine.process_pdf(SRC, job(print_mode="mono_laser", out_mode="color", out_dir=d4))
pngs = sorted(d4.glob("*.png"))
if pngs:
    im = Image.open(pngs[0])
    mx, avg = chroma_of(pngs[0])
    check("仍输出 RGB 三通道", im.mode == "RGB", f"实际 {im.mode}")
    check("但彩度很低（灰墨约束未失效）", mx < 45,
          f"最大彩度 {mx}，平均 {avg:.1f}（应只来自纸张暖调+白平衡）")

print()
print("=" * 72)
print("⑤ PDF 输出内嵌的图像也应是彩色")
print("=" * 72)
d5 = tmp / "c5"
r = engine.process_pdf(SRC, job(print_mode="color_inkjet", out_mode="color",
                                fmt="pdf", out_dir=d5))
pdfs = sorted(d5.glob("*.pdf"))
check("产出了 PDF", bool(pdfs), f"{[p.name for p in pdfs]}")
if pdfs:
    import fitz
    doc = fitz.open(pdfs[0])
    page = doc.load_page(0)
    pix = page.get_pixmap(dpi=72)
    doc.close()
    im = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    a = np.asarray(im, dtype=np.int16)
    mx = int((a.max(axis=2) - a.min(axis=2)).max())
    check("PDF 内嵌图像是彩色", mx > 60, f"最大彩度 {mx}")

print()
print("=" * 72)
print("⑥ 彩色专用参数的调节确实生效")
print("=" * 72)
base = dict(print_mode="color_laser", out_mode="color")
res = {}
for name, kw in (("默认", {}),
                 ("纸张暖调=0", {"paper_warmth": 0.0}),
                 ("纸张暖调=1", {"paper_warmth": 1.0}),
                 ("饱和度=0.5", {"sat_boost": 0.5}),
                 ("饱和度=1.5", {"sat_boost": 1.5})):
    d = tmp / f"p_{name.replace('=', '_')}"
    engine.process_pdf(SRC, job(out_dir=d, color_cast=0.0, **{**base, **kw}))
    ps = sorted(d.glob("*.png"))
    if ps:
        a = np.asarray(Image.open(ps[0]).convert("RGB"), dtype=np.float32)
        lum = a @ np.array([0.299, 0.587, 0.114], np.float32)
        res[name] = (float(lum.mean()),
                     float((a.max(axis=2) - a.min(axis=2)).mean()))

if "纸张暖调=0" in res and "纸张暖调=1" in res:
    r0 = np.asarray(Image.open(sorted((tmp / "p_纸张暖调_0").glob("*.png"))[0]).convert("RGB"), np.float32)
    r1 = np.asarray(Image.open(sorted((tmp / "p_纸张暖调_1").glob("*.png"))[0]).convert("RGB"), np.float32)
    # 暖调应让 R 相对 B 抬高
    d0 = float((r0[..., 0] - r0[..., 2]).mean())
    d1_ = float((r1[..., 0] - r1[..., 2]).mean())
    check("纸张暖调增大 → R 相对 B 抬高", d1_ > d0,
          f"R−B: 暖调0 → {d0:+.2f}，暖调1 → {d1_:+.2f}")

if "饱和度=0.5" in res and "饱和度=1.5" in res:
    check("饱和度增益增大 → 平均彩度上升",
          res["饱和度=1.5"][1] > res["饱和度=0.5"][1],
          f"彩度: 0.5→{res['饱和度=0.5'][1]:.1f}，1.5→{res['饱和度=1.5'][1]:.1f}")

print()
print("=" * 72)
print(f"结果：{'全部通过 ✓' if not FAILED else f'{len(FAILED)} 项失败 ✗'}")
for f in FAILED:
    print(f"   ✗ {f}")
print(f"（测试产物在 {tmp}）")
print("=" * 72)
sys.exit(1 if FAILED else 0)