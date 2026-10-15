# -*- coding: utf-8 -*-
"""
保真度验证：预览（低分辨率 + DPI 补偿）vs 正式输出（200 DPI）

为什么不用逐像素差：
    低分辨率直接渲染 与 高分辨率降采样 在文字边缘会有亚像素级错位，
    逐像素差会被边缘像素放大到毫无意义（实测平均差能到 190+）。

改用**边缘锐度**（梯度能量）与**细节带能量**（原始图 - 轻模糊图）：
    两张图都在同一个最终分辨率下比较，所以指标可直比。
    · 补偿正确 → 预览与真实输出的「糊度」一致，两项指标接近
    · 补偿缺失 → 预览会明显更糊，锐度指标显著偏低
"""
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from app import effects, engine, logsetup                # noqa: E402
from app.config import (LayoutParams, OutputParams,      # noqa: E402
                        PrintParams, RenderJob, ScanParams)

logsetup.init_logging()
SRC = ROOT / "test" / "sample.pdf"
TARGET_LONG_SIDE = 1100


def build(mode, blur, noise, skew, max_side, compensate=True):
    job = RenderJob(
        print_params=PrintParams(mode=mode),
        scan_params=ScanParams(blur=blur, noise=noise, skew_deg=skew, skew_random=0.0),
        layout_params=LayoutParams(paper="A4", margin_mm=8),
        output_params=OutputParams(render_dpi=200, out_dpi=200))
    s = engine.PreviewSession(SRC, job, page_index=0, max_side=max_side)
    scale = s.dpi_scale if compensate else 1.0
    img = effects.apply_scan_effect(s.base_gray, job.scan_params,
                                    np.random.default_rng(s.seed), dpi_scale=scale)
    return img, s


def metrics(a: np.ndarray):
    """边缘锐度 + 细节带能量（只在有内容的区域统计）"""
    f = a.astype(np.float32)

    # 内容掩膜：先重模糊去掉噪点，再找非白区域
    smooth = np.asarray(Image.fromarray(a, "L").filter(ImageFilter.GaussianBlur(2.0)),
                        dtype=np.float32)
    mask = smooth < 235
    if mask.sum() < 2000:
        mask = np.ones_like(smooth, dtype=bool)

    gy, gx = np.gradient(f)
    grad = np.sqrt(gx ** 2 + gy ** 2)
    sharp = float(grad[mask].mean())

    band = np.abs(f - np.asarray(
        Image.fromarray(a, "L").filter(ImageFilter.GaussianBlur(1.2)), dtype=np.float32))
    detail = float(band[mask].mean())
    return sharp, detail, int(mask.sum())


def compare(mode, blur, noise, skew, compensate=True):
    low, s_low = build(mode, blur, noise, skew, TARGET_LONG_SIDE, compensate)
    high, _ = build(mode, blur, noise, skew, 2400, compensate)

    ref = np.asarray(
        Image.fromarray(high, "L").resize((low.shape[1], low.shape[0]), Image.LANCZOS),
        dtype=np.uint8)

    s1, d1, npx = metrics(low)
    s2, d2, _ = metrics(ref)
    tag = "补偿" if compensate else "不补偿"
    print(f"  [{tag}] {mode:12s} blur={blur:.2f} noise={noise:.2f} skew={skew:+.2f} "
          f"| 预览{s_low.info['preview_dpi']:.0f}DPI | 锐度 预览{s1:6.2f}/真值{s2:6.2f} "
          f"= {s1 / s2:5.3f} | 细节带 {d1:5.2f}/{d2:5.2f} = {d1 / d2:5.3f}")
    return s1 / s2, d1 / d2


CASES = [("mono_laser", 0.45, 0.30, 0.0),
         ("mono_laser", 1.60, 0.30, 0.0),
         ("mono_laser", 0.10, 0.30, 0.0),
         ("mono_laser", 0.45, 0.30, 1.20),
         ("color_laser", 0.55, 0.34, 0.0),
         ("color_inkjet", 0.55, 0.34, 0.0)]

print("=== 开启 DPI 补偿（当前实现）===")
ok = [compare(*c, compensate=True) for c in CASES]

print("\n=== 关闭 DPI 补偿（修复前的行为，作为对照）===")
bad = [compare(*c, compensate=False) for c in CASES]

import statistics as st
ok_s = st.mean(abs(x[0] - 1) for x in ok)
ok_d = st.mean(abs(x[1] - 1) for x in ok)
bad_s = st.mean(abs(x[0] - 1) for x in bad)
bad_d = st.mean(abs(x[1] - 1) for x in bad)

print(f"\n锐度比偏离 1.0：  补偿后 {ok_s:.3f}   未补偿 {bad_s:.3f}")
print(f"细节带偏离 1.0：  补偿后 {ok_d:.3f}   未补偿 {bad_d:.3f}")
print("结论:", "DPI 补偿有效，预览与最终输出观感一致"
      if ok_s < bad_s * 0.6 else "补偿未达预期，需要复查")
