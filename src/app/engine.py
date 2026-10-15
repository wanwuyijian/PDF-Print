# -*- coding: utf-8 -*-
"""
处理引擎：PDF → 逐页渲染 → 打印效果 → 扫描效果 → 输出 PDF / 图片

单页处理链路：
    ① 源页按 render_dpi 渲染成 RGB 位图            （= 原始电子文档）
    ② 按目标纸张尺寸建立整张纸的画布，把内容排进可打印区  （= 送进打印机的纸）
    ③ apply_print_effect                       （= 打印机上墨后的纸）
    ④ apply_scan_effect                        （= 扫描仪读回来的灰度）
    ⑤ 重采样到 out_dpi，编码进目标尺寸的 PDF 页
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

import numpy as np
from PIL import Image

try:                                    # PyMuPDF 1.24+
    import pymupdf as fitz
except ImportError:                     # 兼容旧别名
    import fitz  # type: ignore

from . import effects, paper
from .config import RenderJob
from .logsetup import get_logger, log_exception, set_context, clear_context

log = get_logger("app.engine")

# 单页位图超过该像素数时给出警告（约 60MP ≈ 内存 700MB 级别）
BIG_PAGE_PX = 60_000_000


# --------------------------------------------------------------------------- #
# 结果对象
# --------------------------------------------------------------------------- #
@dataclass
class ProcessResult:
    source: str
    ok: bool
    elapsed: float = 0.0
    pages_done: int = 0
    outputs: list[str] = None          # type: ignore[assignment]
    error: str = ""

    def __post_init__(self):
        if self.outputs is None:
            self.outputs = []


class Cancelled(Exception):
    """用户主动取消"""


# --------------------------------------------------------------------------- #
# 页码解析
# --------------------------------------------------------------------------- #
def parse_page_range(spec: str, total: int) -> list[int]:
    """
    "1-3,5,8-10" -> [0,1,2,4,7,8,9]（0 基，去重升序）
    "all" / "" / "全部" -> 全部页码
    """
    spec = (spec or "").strip().lower()
    if spec in ("", "all", "全部", "*"):
        return list(range(total))

    picked: set[int] = set()
    for chunk in spec.replace("，", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            a, _, b = chunk.partition("-")
            try:
                start, end = int(a), int(b)
            except ValueError:
                continue
            if start > end:
                start, end = end, start
            for p in range(max(1, start), min(total, end) + 1):
                picked.add(p - 1)
        else:
            try:
                p = int(chunk)
            except ValueError:
                continue
            if 1 <= p <= total:
                picked.add(p - 1)
    return sorted(picked)


# --------------------------------------------------------------------------- #
# PDF 读取
# --------------------------------------------------------------------------- #
def open_pdf(path: str | Path) -> "fitz.Document":
    """打开 PDF（自动尝试空密码解密），失败抛异常"""
    doc = fitz.open(str(path))
    if doc.needs_pass:
        if not doc.authenticate(""):
            doc.close()
            raise RuntimeError("PDF 已加密，需要密码才能打开")
    if doc.page_count == 0:
        doc.close()
        raise RuntimeError("PDF 没有任何页面")
    return doc


def page_size_mm(page) -> tuple[float, float]:
    """页面物理尺寸（毫米），已考虑 /Rotate"""
    r = page.rect
    return paper.pt_to_mm(r.width), paper.pt_to_mm(r.height)


def render_page_rgb(page, dpi: float) -> Image.Image:
    """把 PDF 页渲染成 RGB 位图（正确处理 /Rotate，防呆校验宽高方向）"""
    mat = fitz.Matrix(dpi / 72.0, dpi / 72.0)
    pix = page.get_pixmap(matrix=mat, alpha=False, colorspace=fitz.csRGB)
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)

    # 防呆：极少数 PDF 的 /Rotate 不会被渲染矩阵带出，宽高方向会转置，这里补一次
    rect = page.rect
    want_land = rect.width > rect.height
    got_land = img.width > img.height
    rotation = int(getattr(page, "rotation", 0) or 0)
    if want_land != got_land and rotation % 180 == 90:
        img = img.rotate(-90 if rotation == 90 else 90, expand=True)
        log.debug("补正页面旋转 /Rotate=%d", rotation)
    return img


# --------------------------------------------------------------------------- #
# 纸张合成
# --------------------------------------------------------------------------- #
def compose_sheet(content: Image.Image,
                  sheet_w_px: int, sheet_h_px: int,
                  margin_px: int,
                  scale_mode: str) -> np.ndarray:
    """
    把内容排到纸张画布的可打印区，返回 float32 (H,W,3)，1.0 = 白纸。

    scale_mode:
        fit    —— 等比缩放到完整放入可打印区，居中
        fill   —— 等比放大铺满可打印区，超出裁剪
        actual —— 1:1 放置（内容按其物理尺寸落在纸上），超出裁剪
    """
    cw = max(1, sheet_w_px - 2 * margin_px)
    ch = max(1, sheet_h_px - 2 * margin_px)

    src_w, src_h = content.size
    if scale_mode == "actual":
        new_w, new_h = src_w, src_h
    elif scale_mode == "fill":
        s = max(cw / src_w, ch / src_h)
        new_w, new_h = max(1, int(round(src_w * s))), max(1, int(round(src_h * s)))
    else:  # fit
        s = min(cw / src_w, ch / src_h)
        new_w, new_h = max(1, int(round(src_w * s))), max(1, int(round(src_h * s)))

    if (new_w, new_h) != (src_w, src_h):
        resample = Image.LANCZOS if new_w < src_w else Image.BICUBIC
        content = content.resize((new_w, new_h), resample)

    # 在纸张上居中，可打印区外的部分直接裁掉
    off_x = margin_px + (cw - new_w) // 2
    off_y = margin_px + (ch - new_h) // 2

    sheet = np.ones((sheet_h_px, sheet_w_px, 3), dtype=np.float32)

    # 计算需要拷贝的重叠区域
    sx0, sy0 = max(0, -off_x), max(0, -off_y)
    dx0, dy0 = max(0, off_x), max(0, off_y)
    copy_w = min(new_w - sx0, sheet_w_px - dx0)
    copy_h = min(new_h - sy0, sheet_h_px - dy0)
    if copy_w <= 0 or copy_h <= 0:
        return sheet

    arr = np.asarray(content, dtype=np.float32)[sy0:sy0 + copy_h, sx0:sx0 + copy_w] / 255.0
    sheet[dy0:dy0 + copy_h, dx0:dx0 + copy_w] = arr
    return sheet


# --------------------------------------------------------------------------- #
# 编码
# --------------------------------------------------------------------------- #
def encode_image(gray: np.ndarray, use_jpeg: bool, quality: int) -> tuple[bytes, str]:
    """灰度数组 → 编码字节 + 扩展名"""
    img = Image.fromarray(gray, mode="L")
    buf = BytesIO()
    if use_jpeg:
        img.save(buf, format="JPEG", quality=int(np.clip(quality, 5, 100)),
                 optimize=True, subsampling=0)
        return buf.getvalue(), ".jpg"
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue(), ".png"


def resample_gray(gray: np.ndarray, scale: float) -> np.ndarray:
    """按比例重采样扫描件（降采样前轻微模糊，模拟扫描采样抗锯齿）"""
    if abs(scale - 1.0) < 0.004:
        return gray
    h, w = gray.shape[:2]
    nw, nh = max(1, int(round(w * scale))), max(1, int(round(h * scale)))
    img = Image.fromarray(gray, mode="L")
    if scale < 1.0:
        img = img.filter(ImageFilter_Gaussian(0.45))
        img = img.resize((nw, nh), Image.LANCZOS)
    else:
        img = img.resize((nw, nh), Image.BICUBIC)
    return np.asarray(img, dtype=np.uint8)


def ImageFilter_Gaussian(radius: float):
    from PIL import ImageFilter
    return ImageFilter.GaussianBlur(radius)


# --------------------------------------------------------------------------- #
# 主处理函数
# --------------------------------------------------------------------------- #
def process_pdf(src_path: str | Path,
                job: RenderJob,
                out_dir: str | Path | None = None,
                progress=None,
                should_cancel=None) -> ProcessResult:
    """
    处理一个 PDF。

    :param progress:      callable(done:int, total:int, msg:str) 进度回调
    :param should_cancel: callable() -> bool 返回 True 时中断（抛 Cancelled）
    :return: ProcessResult
    """
    src_path = Path(src_path)
    t0 = time.perf_counter()
    out_dir = Path(out_dir) if out_dir else None

    def _tick(done: int, total: int, msg: str) -> None:
        if progress:
            try:
                progress(done, total, msg)
            except Exception:
                pass

    def _check_cancel() -> None:
        if should_cancel and should_cancel():
            raise Cancelled()

    pp, sp, lp, op = (job.print_params, job.scan_params,
                      job.layout_params, job.output_params)

    set_context("源文件", str(src_path))
    set_context("参数", job.describe())
    log.info("-" * 70)
    log.info("开始处理: %s", src_path.name)
    log.info("参数: %s", job.describe())

    doc = None
    out_pdf = None
    try:
        doc = open_pdf(src_path)
        indices = parse_page_range(job.pages, doc.page_count)
        if not indices:
            doc.close()
            return ProcessResult(str(src_path), False, 0.0, 0,
                                 [], "页码范围无效或为空")

        # 输出目录 / 文件名
        first_page = doc.load_page(indices[0])
        src_size_mm = page_size_mm(first_page)
        if out_dir is None:
            out_dir = Path(op.out_dir) if op.out_dir else None
        if out_dir is None:
            from .paths import default_output_dir
            out_dir = default_output_dir()
        out_dir.mkdir(parents=True, exist_ok=True)

        base = src_path.stem + (op.name_suffix or "")
        want_pdf = op.out_format in ("pdf", "pdf_img")
        want_img = op.out_format in ("png", "jpg", "pdf_img")
        img_ext = ".png" if op.out_format == "png" else ".jpg"
        if op.out_format == "jpg":
            img_ext = ".jpg"

        pdf_path = out_dir / f"{base}.pdf"
        if want_pdf:
            out_pdf = fitz.open()

        # 随机种子
        if op.seed:
            rng = np.random.default_rng(int(op.seed))
            log.info("随机种子固定为 %d（效果可复现）", op.seed)
        else:
            rng = np.random.default_rng()
            log.info("随机种子: 自动（每次不同）")

        done = 0
        total = len(indices)
        outputs: list[str] = []

        for order, pidx in enumerate(indices):
            _check_cancel()
            page = doc.load_page(pidx)
            src_w_mm, src_h_mm = page_size_mm(page)
            sheet_w_mm, sheet_h_mm = paper.resolve_paper_size(
                lp.paper, lp.custom_w_mm, lp.custom_h_mm,
                lp.orientation, (src_w_mm, src_h_mm))

            margin_mm = paper.clamp_margin(lp.margin_mm, sheet_w_mm, sheet_h_mm)
            sheet_w_px = paper.mm_to_px(sheet_w_mm, op.render_dpi)
            sheet_h_px = paper.mm_to_px(sheet_h_mm, op.render_dpi)
            margin_px = paper.mm_to_px(margin_mm, op.render_dpi)

            if sheet_w_px * sheet_h_px > BIG_PAGE_PX:
                log.warning("第 %d 页位图较大: %d×%d px @%ddpi，处理会较慢",
                            pidx + 1, sheet_w_px, sheet_h_px, op.render_dpi)

            _tick(done, total, f"第 {order + 1}/{total} 页：渲染中…")
            tp = time.perf_counter()
            content = render_page_rgb(page, op.render_dpi)
            log.info("第 %d 页 渲染完成 %dx%d (源页 %.1f×%.1fmm) 用时 %.2fs",
                     pidx + 1, content.width, content.height,
                     src_w_mm, src_h_mm, time.perf_counter() - tp)

            sheet = compose_sheet(content, sheet_w_px, sheet_h_px, margin_px, lp.scale_mode)
            del content

            _tick(done, total,
                  f"第 {order + 1}/{total} 页：模拟{pp.mode_label.split('（')[0]}…")
            tp = time.perf_counter()
            gray = effects.simulate(sheet, pp, sp, rng,
                                    log_hook=lambda m: log.debug("%s", m.strip()))
            del sheet
            log.info("第 %d 页 打印+扫描模拟完成 %.2fs", pidx + 1, time.perf_counter() - tp)

            # 扫描分辨率重采样
            if op.out_dpi != op.render_dpi:
                gray = resample_gray(gray, op.out_dpi / float(op.render_dpi))

            _tick(done, total, f"第 {order + 1}/{total} 页：编码输出…")

            # 二值/无损场景用 PNG，避免 JPEG 在纯黑白图上产生振铃
            use_jpeg = bool(op.jpeg_in_pdf) and not sp.is_binary and op.out_format != "png"
            data, _ext = encode_image(gray, use_jpeg, sp.jpeg_quality)

            if want_pdf and out_pdf is not None:
                pw_pt = paper.mm_to_pt(sheet_w_mm)
                ph_pt = paper.mm_to_pt(sheet_h_mm)
                newp = out_pdf.new_page(width=pw_pt, height=ph_pt)
                newp.insert_image(fitz.Rect(0, 0, pw_pt, ph_pt), stream=data)

            if want_img:
                ipath = out_dir / f"{base}_p{order + 1:03d}{img_ext}"
                with open(ipath, "wb") as fh:
                    fh.write(data)
                outputs.append(str(ipath))
                log.info("第 %d 页 图片已保存: %s", pidx + 1, ipath.name)

            del gray, data
            done += 1

        if want_pdf and out_pdf is not None:
            _check_cancel()
            out_pdf.set_metadata({
                "title": f"{src_path.stem}{op.name_suffix}",
                "producer": "PDF 打印扫描模拟器",
                "creator": "PDF 打印扫描模拟器",
            })
            out_pdf.save(str(pdf_path), garbage=4, deflate=True)
            outputs.insert(0, str(pdf_path))
            log.info("PDF 已保存: %s (%.1f KB)", pdf_path, pdf_path.stat().st_size / 1024)

        elapsed = time.perf_counter() - t0
        log.info("处理完成: %s  共 %d 页  用时 %.2fs", src_path.name, done, elapsed)
        _tick(total, total, "完成")
        return ProcessResult(str(src_path), True, elapsed, done, outputs)

    except Cancelled:
        log.warning("用户取消: %s", src_path.name)
        raise
    except Exception as e:
        log_exception(log, f"处理失败: {src_path}")
        return ProcessResult(str(src_path), False,
                             time.perf_counter() - t0, 0, [], f"{type(e).__name__}: {e}")
    finally:
        clear_context("源文件", "参数")
        try:
            if out_pdf is not None:
                out_pdf.close()
        except Exception:
            pass
        try:
            if doc is not None:
                doc.close()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# 预览会话（供「实时调参」使用）
# --------------------------------------------------------------------------- #
class PreviewSession:
    """
    把「PDF 渲染 → 排版 → 打印效果」一次算完并缓存下来，之后只重跑**扫描阶段**。

    为什么要这样切分：
        渲染 PDF 页 + 打印模拟 约 150~300ms，扫描模拟只要 30~80ms。
        把前面的结果缓存住，模糊度/噪点/倾斜度这类**扫描侧参数**就能做到滑动时近乎实时刷新。

    另外：整个会话使用**固定随机种子**，这样每次重算时纸底云斑、噪点图案的位置
    完全不变，只有强度在变 —— 调参时画面不会乱跳，才能看清楚参数本身的影响。
    """

    DEFAULT_SEED = 20261014

    def __init__(self, src_path: str | Path, job: RenderJob,
                 page_index: int = 0, max_side: int = 860,
                 seed: int | None = None):
        self.src = Path(src_path)
        self.page_index = page_index
        self.seed = int(seed) if seed else self.DEFAULT_SEED
        self.info: dict = {}

        doc = open_pdf(self.src)
        try:
            if page_index < 0:
                page_index = 0
            if page_index >= doc.page_count:
                page_index = doc.page_count - 1
            self.page_index = page_index
            page = doc.load_page(page_index)

            src_w_mm, src_h_mm = page_size_mm(page)
            lp, op = job.layout_params, job.output_params
            sheet_w_mm, sheet_h_mm = paper.resolve_paper_size(
                lp.paper, lp.custom_w_mm, lp.custom_h_mm,
                lp.orientation, (src_w_mm, src_h_mm))

            # 预览分辨率：把长边压到 max_side 以内
            need = max(sheet_w_mm, sheet_h_mm) / 25.4 * op.render_dpi
            dpi = max(60.0, op.render_dpi * min(1.0, max_side / max(1.0, need)))

            margin_mm = paper.clamp_margin(lp.margin_mm, sheet_w_mm, sheet_h_mm)
            sw = paper.mm_to_px(sheet_w_mm, dpi)
            sh = paper.mm_to_px(sheet_h_mm, dpi)
            mp = paper.mm_to_px(margin_mm, dpi)

            content = render_page_rgb(page, dpi)
            sheet = compose_sheet(content, sw, sh, mp, lp.scale_mode)
            del content

            # DPI 补偿：预览分辨率通常低于正式输出的渲染分辨率。
            # 墨点扩散、光学模糊、云斑尺度都是按像素定义的量，
            # 不做补偿的话预览会比真实输出「糊好几倍」，调参就失去意义。
            self.dpi_scale = dpi / float(op.render_dpi)

            # 打印阶段：只算一次
            rng = np.random.default_rng(self.seed)
            self.base_gray = effects.print_effect_grayscale(
                sheet, job.print_params, rng, dpi_scale=self.dpi_scale)
            del sheet

            self.info = {
                "page_index": page_index,
                "page_count": doc.page_count,
                "preview_dpi": dpi,
                "render_dpi": op.render_dpi,
                "dpi_scale": self.dpi_scale,
                "sheet_mm": (sheet_w_mm, sheet_h_mm),
                "src_mm": (src_w_mm, src_h_mm),
                "px": self.base_gray.shape[:2],
            }
        finally:
            doc.close()

    def rescan(self, scan_params: ScanParams, scale: float = 1.0) -> np.ndarray:
        """
        仅重跑扫描阶段，返回 uint8 灰度图。

        :param scale: <1 时把缓存的打印件先降采样再扫描，用于拖动滑块过程中的
                      **快速预览**（分辨率降一档，速度提升约 2.5 倍）。
                      dpi_scale 会同步乘以 scale，所以模糊/云斑的物理尺寸依然正确。
        """
        base = self.base_gray
        dpi_scale = getattr(self, "dpi_scale", 1.0)

        if 0.15 < scale < 0.995:
            h, w = base.shape[:2]
            nw, nh = max(1, int(round(w * scale))), max(1, int(round(h * scale)))
            u8 = np.clip(base * 255.0, 0, 255).astype(np.uint8)
            small = Image.fromarray(u8, mode="L").resize((nw, nh), Image.BILINEAR)
            base = np.asarray(small, dtype=np.float32) / 255.0
            dpi_scale *= scale

        rng = np.random.default_rng(self.seed)
        return effects.apply_scan_effect(base, scan_params, rng, dpi_scale=dpi_scale)


def render_preview(src_path: str | Path, job: RenderJob,
                   page_index: int = 0,
                   max_side: int = 900) -> Image.Image:
    """
    生成单页预览图（一次性），自动限制尺寸以保证速度。
    """
    session = PreviewSession(src_path, job, page_index=page_index, max_side=max_side)
    return Image.fromarray(session.rescan(job.scan_params), mode="L")


def pdf_page_count(path: str | Path) -> int:
    try:
        doc = fitz.open(str(path))
        n = doc.page_count
        doc.close()
        return n
    except Exception:
        return 0
