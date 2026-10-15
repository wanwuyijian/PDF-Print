# -*- coding: utf-8 -*-
"""纸张尺寸表与单位换算"""

from __future__ import annotations

MM_PER_INCH = 25.4
PT_PER_INCH = 72.0

# 名称 -> (宽mm, 高mm)，均为纵向（portrait）定义
PAPER_SIZES: dict[str, tuple[float, float]] = {
    "A3":      (297.0, 420.0),
    "A4":      (210.0, 297.0),
    "A5":      (148.0, 210.0),
    "A6":      (105.0, 148.0),
    "B4":      (250.0, 353.0),     # ISO B4
    "B5":      (176.0, 250.0),     # ISO B5
    "16K":     (185.0, 260.0),     # 大16开
    "32K":     (130.0, 185.0),
    "Letter":  (215.9, 279.4),
    "Legal":   (215.9, 355.6),
    "38x26mm": (38.0, 26.0),       # 证件卡（驾照/身份证扫描件）
    "54x86mm": (54.0, 86.0),       # 银行卡 / 名片
}

# 供下拉框使用的显示名
PAPER_LABELS: dict[str, str] = {
    "source": "保持原页面尺寸",
    "custom": "自定义尺寸…",
}
for _k, (_w, _h) in PAPER_SIZES.items():
    PAPER_LABELS[_k] = f"{_k}  ({_w:g} × {_h:g} mm)"

PAPER_ORDER = ["source", "A3", "A4", "A5", "A6", "B4", "B5", "16K", "32K",
               "Letter", "Legal", "54x86mm", "38x26mm", "custom"]


def mm_to_pt(mm: float) -> float:
    return mm / MM_PER_INCH * PT_PER_INCH


def pt_to_mm(pt: float) -> float:
    return pt / PT_PER_INCH * MM_PER_INCH


def mm_to_px(mm: float, dpi: float) -> int:
    return max(1, int(round(mm / MM_PER_INCH * dpi)))


def px_to_mm(px: float, dpi: float) -> float:
    return px / dpi * MM_PER_INCH


def resolve_paper_size(paper: str,
                       custom_w_mm: float,
                       custom_h_mm: float,
                       orientation: str = "auto",
                       source_size_mm: tuple[float, float] | None = None,
                       ) -> tuple[float, float]:
    """
    计算目标纸张物理尺寸（毫米，纵向口径）。

    paper:
        "source"         -> 使用原 PDF 页面尺寸
        见 PAPER_SIZES   -> 标准纸
        "custom"         -> custom_w_mm × custom_h_mm
    orientation:
        "auto"      -> 由 source_size_mm 的横竖决定（内容为横向则纸张也横向）
        "portrait"  -> 强制纵向
        "landscape" -> 强制横向
    返回 (宽mm, 高mm)，已按 orientation 应用横竖。
    """
    if paper == "source":
        if not source_size_mm:
            w_mm, h_mm = PAPER_SIZES["A4"]
        else:
            w_mm, h_mm = source_size_mm
    elif paper == "custom":
        w_mm = max(10.0, min(2000.0, float(custom_w_mm)))
        h_mm = max(10.0, min(2000.0, float(custom_h_mm)))
    else:
        w_mm, h_mm = PAPER_SIZES.get(paper, PAPER_SIZES["A4"])

    # 统一成纵向口径再决定方向
    src_landscape = w_mm > h_mm
    w_port, h_port = (h_mm, w_mm) if src_landscape else (w_mm, h_mm)

    if orientation == "portrait":
        return w_port, h_port
    if orientation == "landscape":
        return h_port, w_port

    # auto：按内容方向（source 用自身方向；标准纸用源页面方向）
    if source_size_mm:
        return (h_port, w_port) if source_size_mm[0] > source_size_mm[1] else (w_port, h_port)
    return (h_port, w_port) if src_landscape else (w_port, h_port)


def clamp_margin(margin_mm: float, w_mm: float, h_mm: float) -> float:
    """边距不能大到把可打印区吃掉"""
    limit = min(w_mm, h_mm) / 2.0 - 2.0
    return max(0.0, min(float(margin_mm), max(0.0, limit)))
