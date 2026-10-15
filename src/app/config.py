# -*- coding: utf-8 -*-
"""参数定义与内置预设"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict, fields
from typing import Any


# =========================================================================== #
# 打印效果参数
# =========================================================================== #
PRINT_MODES = {
    "mono_laser":   "黑白激光打印机（单色碳粉）",
    "color_laser":  "彩色激光打印机（CMYK 四色叠印）",
    "color_inkjet": "彩色喷墨打印机（染料墨水）",
}

# 各打印引擎的物理特征默认值：反射率下限 / 墨点扩散 / 色彩串扰
ENGINE_TRAITS = {
    #            暗部反射率下限  墨扩散sigma  串扰px  彩度增益  黑偏色(RGB偏移)
    "mono_laser":   (0.045, 0.40, 0.0, 1.00, (0.00, 0.00, 0.00)),
    "color_laser":  (0.170, 0.55, 0.65, 1.06, (-0.012, 0.000, 0.020)),  # 合成黑偏冷偏青
    "color_inkjet": (0.230, 0.95, 0.35, 1.18, (-0.030, -0.006, 0.034)),  # 喷墨偏青、暗部发闷
}


@dataclass
class PrintParams:
    """打印效果参数"""
    mode: str = "mono_laser"        # PRINT_MODES 的键

    # 浓度：1.0 为标准。>1 表示碳粉/墨量更足（印得更黑更实），<1 表示淡印
    density: float = 1.00
    # 对比度：打印机成像的阶调对比，1.0 标准
    contrast: float = 1.00
    # 墨点扩散强度 0~1（越大越糊，模拟纸纤维毛细渗透 / 定影不足）
    spread: float = 0.35
    # 颗粒感 0~1（碳粉颗粒 / 墨滴不均）
    grain: float = 0.30
    # 半色调网点强度 0~1（打印机用网点表现灰阶）
    halftone: float = 0.25
    # 色彩串扰 0~1（四色套印不准导致的彩边，仅彩色模式有效）
    misregister: float = 0.50
    # 总墨量上限 100~400(%)，越高暗部越糊（仅彩色模式有效）
    tac_limit: float = 280.0

    def trait(self) -> tuple:
        return ENGINE_TRAITS.get(self.mode, ENGINE_TRAITS["mono_laser"])

    @property
    def is_mono(self) -> bool:
        return self.mode == "mono_laser"

    @property
    def mode_label(self) -> str:
        return PRINT_MODES.get(self.mode, self.mode)


# =========================================================================== #
# 扫描效果参数
# =========================================================================== #
@dataclass
class ScanParams:
    """扫描效果参数（最终产出为黑白/灰度扫描件）"""
    # 纸张底色灰度（255=纯白。真实纸基通常 238~251）
    paper_base: float = 246.0
    # 纸张底色不均匀（云斑/纤维纹理）0~1
    paper_cloud: float = 0.35
    # 传感器噪点 0~1
    noise: float = 0.30
    # 扫描光学模糊 sigma（px）0~2
    blur: float = 0.45
    # 对比度 / 亮度（模拟扫描仪自动曝光）
    contrast: float = 1.07
    brightness: float = 1.00
    # 进纸倾斜角度（度）。None = 每页随机 ±skew_random
    skew_deg: float | None = None
    skew_random: float = 0.70
    # 边缘暗角/装订阴影 0~1
    vignette: float = 0.25
    # JPEG 压缩质量（1~100），越低数码味越重、块效应越明显
    jpeg_quality: int = 78
    # 输出模式：gray=灰度扫描；binary=黑白二值扫描（纯黑白，字口硬化）
    out_mode: str = "gray"
    # 二值化阈值（out_mode=binary 时生效）
    binary_threshold: int = 158

    @property
    def is_binary(self) -> bool:
        return self.out_mode == "binary"


# =========================================================================== #
# 版式 / 输出参数
# =========================================================================== #
SCALE_MODES = {
    "fit":    "适应页面（等比缩放，完整放入可打印区）",
    "fill":   "填满页面（等比放大铺满，超出部分裁掉）",
    "actual": "原尺寸（1:1 放置，超出部分裁掉）",
}

ORIENTATIONS = {
    "auto":      "自动（跟随原页面方向）",
    "portrait":  "纵向",
    "landscape": "横向",
}

OUTPUT_FORMATS = {
    "pdf":  "仅 PDF",
    "png":  "仅 PNG 图片",
    "jpg":  "仅 JPG 图片",
    "pdf_img": "PDF + 图片",
}


@dataclass
class LayoutParams:
    """版式参数"""
    paper: str = "A4"                # paper.PAPER_SIZES 的键，或 "source"
    custom_w_mm: float = 210.0       # paper == "custom" 时生效
    custom_h_mm: float = 297.0
    orientation: str = "auto"
    margin_mm: float = 8.0           # 四边留白（模拟打印机不可打印边距 + 扫描裁边）
    scale_mode: str = "fit"


@dataclass
class OutputParams:
    """输出参数"""
    render_dpi: int = 200            # 打印阶段的分辨率
    out_dpi: int = 200               # 扫描阶段的分辨率（低于 render_dpi 会丢失细节，更真实）
    out_format: str = "pdf"
    out_dir: str = ""                # 输出目录；空 = 使用默认 output 目录
    # 可选的输出文件名（不含路径，留空 = 按源文件名自动生成）。
    # 与 out_dir 组合起来就是完整的输出文件路径 —— 界面上的「另存为…」按钮
    # 会一次性把目录和文件名都填好。刻意不把两者合成一个字符串字段：
    # 只靠"有没有扩展名"去猜用户填的是目录还是文件，必然会猜错
    # （用户想写 D:\扫描\合同扫描件 这种无扩展名的文件名时会被误当成目录）。
    out_name: str = ""
    name_suffix: str = "_scan"       # 输出文件名后缀
    jpeg_in_pdf: bool = True         # PDF 内嵌图像使用 JPEG（体积小）
    open_folder_after: bool = False  # 完成后打开输出目录
    # 随机种子：0 = 每次运行都不同（推荐，避免所有页面纹理雷同）；
    # 设为非 0 的固定值可使「噪点/云斑/倾斜」完全可复现，便于对比参数或复现 bug
    seed: int = 0


# =========================================================================== #
# 汇总
# =========================================================================== #
@dataclass
class RenderJob:
    """一次渲染任务的完整参数"""
    print_params: PrintParams = field(default_factory=PrintParams)
    scan_params: ScanParams = field(default_factory=ScanParams)
    layout_params: LayoutParams = field(default_factory=LayoutParams)
    output_params: OutputParams = field(default_factory=OutputParams)
    pages: str = "all"                     # "all" 或 "1-3,5,8-10"

    # ---- 序列化（供预设保存/读取）----
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RenderJob":
        def build(dc, d: dict):
            valid = {f.name for f in fields(dc)}
            return dc(**{k: v for k, v in (d or {}).items() if k in valid})
        return cls(
            print_params=build(PrintParams, data.get("print_params")),
            scan_params=build(ScanParams, data.get("scan_params")),
            layout_params=build(LayoutParams, data.get("layout_params")),
            output_params=build(OutputParams, data.get("output_params")),
            pages=data.get("pages", "all"),
        )

    def describe(self) -> str:
        """单行摘要，写进日志便于复现问题"""
        p, s, l, o = self.print_params, self.scan_params, self.layout_params, self.output_params
        return (
            f"模式={p.mode_label} 浓度={p.density:.2f} 扩散={p.spread:.2f} 颗粒={p.grain:.2f} "
            f"网点={p.halftone:.2f} | 扫描: 纸底={s.paper_base:.0f} 云斑={s.paper_cloud:.2f} "
            f"噪点={s.noise:.2f} 模糊={s.blur:.2f} 对比={s.contrast:.2f} 倾斜={s.skew_deg}±{s.skew_random:.2f} "
            f"暗角={s.vignette:.2f} JPG={s.jpeg_quality} 输出={s.out_mode} | "
            f"纸张={l.paper} {l.orientation} 边距={l.margin_mm}mm 缩放={l.scale_mode} | "
            f"打印DPI={o.render_dpi} 扫描DPI={o.out_dpi} 格式={o.out_format} 页={self.pages}"
        )


# =========================================================================== #
# 内置预设
# =========================================================================== #
def _mk_print(**kw) -> dict:
    return kw


BUILTIN_PRESETS: dict[str, dict] = {
    "标准文档（推荐）": dict(
        print_params=_mk_print(mode="mono_laser", density=1.00, contrast=1.00,
                               spread=0.35, grain=0.30, halftone=0.25),
        scan_params=dict(paper_base=246, paper_cloud=0.35, noise=0.30, blur=0.45,
                         contrast=1.07, brightness=1.00, vignette=0.25,
                         jpeg_quality=78, out_mode="gray"),
        layout_params=dict(paper="A4", orientation="auto", margin_mm=8.0, scale_mode="fit"),
    ),
    "轻印淡扫（干净）": dict(
        print_params=_mk_print(mode="mono_laser", density=0.86, contrast=0.95,
                               spread=0.22, grain=0.16, halftone=0.14),
        scan_params=dict(paper_base=251, paper_cloud=0.18, noise=0.15, blur=0.30,
                         contrast=1.03, brightness=1.02, vignette=0.10,
                         jpeg_quality=90, out_mode="gray"),
        layout_params=dict(paper="A4", orientation="auto", margin_mm=6.0, scale_mode="fit"),
    ),
    "重印浓扫（复印机味）": dict(
        print_params=_mk_print(mode="mono_laser", density=1.18, contrast=1.16,
                               spread=0.70, grain=0.55, halftone=0.42),
        scan_params=dict(paper_base=239, paper_cloud=0.60, noise=0.50, blur=0.85,
                         contrast=1.18, brightness=0.97, vignette=0.42,
                         jpeg_quality=62, out_mode="gray"),
        layout_params=dict(paper="A4", orientation="auto", margin_mm=10.0, scale_mode="fit"),
    ),
    "老旧复印机（脏底）": dict(
        print_params=_mk_print(mode="mono_laser", density=1.10, contrast=1.10,
                               spread=0.85, grain=0.70, halftone=0.50),
        scan_params=dict(paper_base=229, paper_cloud=0.85, noise=0.62, blur=1.05,
                         contrast=1.22, brightness=0.95, vignette=0.55,
                         jpeg_quality=52, out_mode="gray"),
        layout_params=dict(paper="A4", orientation="auto", margin_mm=12.0, scale_mode="fit"),
    ),
    "黑白二值扫描（硬字口）": dict(
        print_params=_mk_print(mode="mono_laser", density=1.05, contrast=1.05,
                               spread=0.30, grain=0.25, halftone=0.20),
        scan_params=dict(paper_base=250, paper_cloud=0.25, noise=0.28, blur=0.50,
                         contrast=1.10, brightness=1.00, vignette=0.20,
                         jpeg_quality=80, out_mode="binary", binary_threshold=158),
        layout_params=dict(paper="A4", orientation="auto", margin_mm=8.0, scale_mode="fit"),
    ),
    "彩色打印→黑白扫描": dict(
        print_params=_mk_print(mode="color_laser", density=1.00, contrast=1.02,
                               spread=0.55, grain=0.30, halftone=0.20,
                               misregister=0.55, tac_limit=280),
        scan_params=dict(paper_base=245, paper_cloud=0.40, noise=0.34, blur=0.55,
                         contrast=1.06, brightness=1.00, vignette=0.28,
                         jpeg_quality=76, out_mode="gray"),
        layout_params=dict(paper="A4", orientation="auto", margin_mm=8.0, scale_mode="fit"),
    ),
}

PRESET_ORDER = list(BUILTIN_PRESETS.keys())
