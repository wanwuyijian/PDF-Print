# -*- coding: utf-8 -*-
"""
自检模式（--selftest）

打包成 exe 之后最常见的两类问题：
  ① 环境问题：依赖没打进去、字体缺失、目录不可写、杀软拦了临时解压目录
  ② 代码/参数问题：某个参数组合下渲染异常

自检把整条链路跑一遍并逐项断言，最后产出一份报告文件。
用户遇到问题只要跑一次自检、把报告发出来，就能立刻区分是哪一类。

命令行：
    PDF打印扫描模拟器_v1.0.0.exe --selftest

报告写入 logs/selftest_<时间戳>.txt
"""

from __future__ import annotations

import datetime as _dt
import platform
import sys
import tempfile
import time as _time
import traceback
from pathlib import Path

import numpy as np

from . import engine, logsetup, paths
from .config import (LayoutParams, OutputParams, PrintParams, RenderJob,
                     ScanParams)
from .version import APP_NAME, __version__

# (名称, 是否通过, 详情)
CHECK_LOG: list[tuple[str, bool, str]] = []


class _Check:
    """
    检查项。用法：

        with _check("依赖 numpy") as c:
            import numpy as np
            c.detail = f"版本 {np.__version__}"

    异常会被吞掉并记为失败，保证后续检查项继续执行。
    """

    def __init__(self, name: str):
        self.name = name
        self.detail = ""

    def __enter__(self) -> "_Check":
        return self

    def __exit__(self, et, ev, tb) -> bool:
        if et is None:
            CHECK_LOG.append((self.name, True, self.detail))
        else:
            CHECK_LOG.append((self.name, False, f"{et.__name__}: {ev}"))
        return True          # 吞掉异常，继续下一项


def _check(name: str) -> _Check:
    return _Check(name)


def _make_test_pdf(path: Path) -> None:
    """生成自检用 PDF：含中文、英文、灰阶、彩色、深色块"""
    import pymupdf as fitz
    doc = fitz.open()
    p = doc.new_page(width=595, height=842)
    p.insert_text((50, 70), "自检文档 中文渲染测试", fontname="china-s", fontsize=20)
    p.insert_text((50, 95), "Self-test AaBbCc 0123456789", fontname="helv", fontsize=11)
    for i in range(11):
        g = (255 - i * 25) / 255
        p.draw_rect(fitz.Rect(50 + i * 45, 120, 50 + i * 45 + 42, 170),
                    color=None, fill=(g, g, g))
    for i, c in enumerate([(1, 0, 0), (0, 0.6, 0), (0, 0, 1), (1, 0.85, 0)]):
        p.draw_rect(fitz.Rect(50 + i * 90, 190, 50 + i * 90 + 80, 250), color=None, fill=c)
    p.draw_rect(fitz.Rect(50, 270, 545, 380), color=None, fill=(0.06, 0.06, 0.08))
    p2 = doc.new_page(width=595, height=842)
    p2.insert_text((50, 70), "第二页 自检", fontname="china-s", fontsize=18)
    p2.insert_text((50, 100), "min font 6pt 测试", fontname="china-s", fontsize=6.5)
    p2.draw_rect(fitz.Rect(50, 140, 545, 300), color=None, fill=(0.05, 0.05, 0.06))
    doc.save(str(path))
    doc.close()


def _gray_stats(path: Path) -> tuple[float, int, int]:
    """读回输出图片，返回 (平均灰度, 最暗值, 像素数)"""
    from PIL import Image
    a = np.asarray(Image.open(path).convert("L"), dtype=np.float32)
    return float(a.mean()), int(a.min()), int(a.size)


def run_selftest() -> tuple[bool, str]:
    """执行自检，返回 (是否全部通过, 报告文本)"""
    t0 = _dt.datetime.now()
    CHECK_LOG.clear()

    # ================= 1. 运行环境 =================
    with _check("Python 版本") as c:
        v = sys.version_info
        assert (v.major, v.minor) >= (3, 10), f"Python {v.major}.{v.minor} 过低"
        c.detail = f"{v.major}.{v.minor}.{v.micro}"

    with _check("运行模式识别") as c:
        c.detail = "打包 exe" if paths.is_frozen() else "源码运行"

    with _check("依赖 PyMuPDF") as c:
        import pymupdf as fitz
        c.detail = f"版本 {fitz.version[0]}"

    with _check("依赖 Pillow") as c:
        import PIL
        c.detail = f"版本 {PIL.__version__}"

    with _check("依赖 numpy") as c:
        c.detail = f"版本 {np.__version__}"

    with _check("Python 带 tkinter（界面依赖）") as c:
        import tkinter
        c.detail = f"Tk {tkinter.TkVersion}"

    with _check("日志目录可写") as c:
        d = paths.log_dir()
        probe = d / ".selftest_write"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        c.detail = str(d)

    with _check("临时目录可写（exe 自解压目录）") as c:
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "x").write_text("1", encoding="utf-8")
            c.detail = td

    # ================= 2. 端到端处理 =================
    tmp = Path(tempfile.mkdtemp(prefix="pdfprint_selftest_"))
    out = tmp / "out"
    out.mkdir(parents=True, exist_ok=True)
    src = tmp / "selftest.pdf"

    with _check("生成含中文的自检 PDF") as c:
        _make_test_pdf(src)
        assert src.exists() and src.stat().st_size > 1000, "文件过小"
        c.detail = f"{src.stat().st_size} 字节"

    # (标签, 参数, 后缀, 输出格式, DPI)
    cases = [
        ("黑白激光 → A4 灰度 PNG", PrintParams(mode="mono_laser"), ScanParams(),
         "mono", "png", 200),
        ("彩色激光 → A4 灰度 PNG", PrintParams(mode="color_laser"), ScanParams(),
         "clr", "png", 200),
        ("彩色喷墨 → A5 二值 PDF", PrintParams(mode="color_inkjet"),
         ScanParams(out_mode="binary"), "ink_a5", "pdf", 300),
        ("黑白激光 → 自定义 100×150mm PDF", PrintParams(mode="mono_laser"), ScanParams(),
         "custom", "pdf", 150),
        ("黑白激光 → A3 横向 灰度 PDF", PrintParams(mode="mono_laser"), ScanParams(),
         "a3", "pdf", 150),
    ]

    produced: dict[str, Path] = {}
    for label, pp, sp, suffix, fmt, dpi in cases:
        with _check(f"处理：{label}") as c:
            lp = LayoutParams(paper="A4", margin_mm=8)
            if suffix == "ink_a5":
                lp = LayoutParams(paper="A5", margin_mm=6)
            elif suffix == "custom":
                lp = LayoutParams(paper="custom", custom_w_mm=100, custom_h_mm=150)
            elif suffix == "a3":
                lp = LayoutParams(paper="A3", orientation="landscape", margin_mm=10)

            job = RenderJob(pp, sp, lp,
                            OutputParams(render_dpi=dpi, out_dpi=dpi, out_format=fmt,
                                         out_dir=str(out), name_suffix=f"_{suffix}",
                                         seed=12345))
            res = engine.process_pdf(src, job, out_dir=out)
            assert res.ok, res.error
            assert res.pages_done == 2, f"页数 {res.pages_done} != 2"
            assert res.outputs, "没有产出文件"
            produced[suffix] = Path(res.outputs[0])
            c.detail = f"{res.pages_done} 页，{len(res.outputs)} 个文件，{res.elapsed:.1f}s"

    # ================= 3. 输出正确性 =================
    for suffix in ("mono", "clr"):
        p = produced.get(suffix)
        if not p:
            continue
        with _check(f"图像合理性：{cases[0 if suffix == 'mono' else 1][0]}") as c:
            mean, mn, n = _gray_stats(p)
            assert n > 10000, f"像素数异常 {n}"
            assert 150 < mean < 255, f"平均灰度 {mean:.1f} 不合理（扫描件应偏白）"
            assert mn < 60, f"最暗值 {mn} 过亮，文字/深色块可能没渲染出来"
            c.detail = f"均值 {mean:.1f}｜最暗 {mn}｜{n} 像素"

    with _check("二值模式确实是纯黑白") as c:
        from PIL import Image as _Image
        job = RenderJob(PrintParams(mode="mono_laser"), ScanParams(out_mode="binary"),
                        LayoutParams(paper="A4", margin_mm=8),
                        OutputParams(render_dpi=200, out_dpi=200, out_format="png",
                                     out_dir=str(out), name_suffix="_bin", seed=3))
        engine.process_pdf(src, job, out_dir=out)
        p = out / "selftest_bin_p001.png"
        assert p.exists(), f"缺少产物 {p.name}"
        a = np.asarray(_Image.open(p).convert("L"))
        vals = sorted(set(np.unique(a).tolist()))
        assert set(vals) <= {0, 255}, f"出现非黑白像素：{vals[:10]}"
        c.detail = f"仅含像素值 {vals}"

    with _check("页面尺寸：A5 = 148×210mm") as c:
        import pymupdf as fitz
        d = fitz.open(str(produced["ink_a5"]))
        r = d[0].rect
        w_mm, h_mm = r.width / 72 * 25.4, r.height / 72 * 25.4
        d.close()
        assert abs(min(w_mm, h_mm) - 148) < 1.0, f"得到 {w_mm:.1f}×{h_mm:.1f}mm"
        assert abs(max(w_mm, h_mm) - 210) < 1.0, f"得到 {w_mm:.1f}×{h_mm:.1f}mm"
        c.detail = f"{w_mm:.1f}×{h_mm:.1f} mm"

    with _check("页面尺寸：自定义 = 100×150mm") as c:
        import pymupdf as fitz
        d = fitz.open(str(produced["custom"]))
        r = d[0].rect
        w_mm, h_mm = r.width / 72 * 25.4, r.height / 72 * 25.4
        d.close()
        assert abs(min(w_mm, h_mm) - 100) < 1.0 and abs(max(w_mm, h_mm) - 150) < 1.0, \
            f"得到 {w_mm:.1f}×{h_mm:.1f}mm"
        c.detail = f"{w_mm:.1f}×{h_mm:.1f} mm"

    with _check("页面尺寸：A3 横向 = 420×297mm") as c:
        import pymupdf as fitz
        d = fitz.open(str(produced["a3"]))
        r = d[0].rect
        w_mm, h_mm = r.width / 72 * 25.4, r.height / 72 * 25.4
        d.close()
        assert abs(max(w_mm, h_mm) - 420) < 1.5 and abs(min(w_mm, h_mm) - 297) < 1.5, \
            f"得到 {w_mm:.1f}×{h_mm:.1f}mm"
        assert w_mm > h_mm, "未按横向输出"
        c.detail = f"{w_mm:.1f}×{h_mm:.1f} mm（横向）"

    with _check("引擎差异：彩色打印的黑应比黑白打印发灰") as c:
        _, mn_mono, _ = _gray_stats(out / "selftest_mono_p002.png")
        _, mn_clr, _ = _gray_stats(out / "selftest_clr_p002.png")
        assert mn_clr > mn_mono + 3, \
            f"彩色最暗 {mn_clr} 未明显浅于黑白最暗 {mn_mono}（合成黑未生效？）"
        c.detail = f"黑白最暗 {mn_mono} ＜ 彩色最暗 {mn_clr}"

    with _check("随机种子可复现") as c:
        job = RenderJob(PrintParams(mode="mono_laser"),
                        ScanParams(skew_random=0.7),
                        LayoutParams(paper="A4", margin_mm=8),
                        OutputParams(render_dpi=150, out_dpi=150, out_format="png",
                                     out_dir=str(out), name_suffix="_seed", seed=4242))
        engine.process_pdf(src, job, out_dir=out)
        first = (out / "selftest_seed_p001.png").read_bytes()
        engine.process_pdf(src, job, out_dir=out)
        second = (out / "selftest_seed_p001.png").read_bytes()
        assert first == second, "同一种子两次输出不一致"
        c.detail = f"{len(first)} 字节，两次完全一致"

    # ================= 4. 页码范围解析（v1.0.0 崩溃回归点）=================
    with _check("页码范围：语法校验不分配内存") as c:
        # v1.0.0 在这里用 parse_page_range(spec, 10**9) 做校验，
        # spec="all" 时会 list(range(10**9)) → 卡死 70 秒后 MemoryError
        # 注意：局部计时变量不要叫 t0 —— 函数开头的 t0 是 _dt.datetime.now()
        _pt = _time.perf_counter()
        ok_all, _ = engine.validate_page_range("all")
        dt = _time.perf_counter() - _pt
        assert ok_all, "'all' 应被判定为合法"
        assert dt < 0.05, f"校验耗时 {dt:.3f}s，说明仍在做无谓的展开"
        c.detail = f"validate_page_range('all') {dt * 1000:.3f} ms"

    with _check("页码范围：荒谬 total 不会撑爆内存") as c:
        _pt = _time.perf_counter()
        r = engine.parse_page_range("all", 10 ** 9)
        dt = _time.perf_counter() - _pt
        assert len(r) <= engine.MAX_PAGE_RANGE, f"返回了 {len(r)} 项"
        assert dt < 1.0, f"耗时 {dt:.2f}s"
        c.detail = f"'all' + total=10^9 → {len(r)} 项，{dt * 1000:.1f} ms（已截断）"

    with _check("页码范围：解析结果正确（1 基 → 0 基）") as c:
        cases = [("all", 3, [0, 1, 2]),
                 ("1-3", 5, [0, 1, 2]),
                 ("3-1", 5, [0, 1, 2]),
                 ("1-99", 5, [0, 1, 2, 3, 4]),
                 ("1-3,5,8-10", 10, [0, 1, 2, 4, 7, 8, 9]),
                 ("1-3，5", 10, [0, 1, 2, 4]),
                 ("9", 5, []),
                 ("0", 5, [])]
        for spec, total, expect in cases:
            got = engine.parse_page_range(spec, total)
            assert got == expect, f"{spec!r},{total} → {got}，应为 {expect}"
        c.detail = f"{len(cases)} 组用例全部正确"

    with _check("页码范围：非法输入被拒绝并提示") as c:
        for bad in ("abc", "1-x", "1--3", "1.5", "1_0", "第3页"):
            ok, why = engine.validate_page_range(bad)
            assert not ok, f"{bad!r} 竟被判定为合法"
            assert why, f"{bad!r} 拒绝时没有给出原因"
        c.detail = "abc / 1-x / 1--3 / 1.5 / 1_0 / 第3页 均被拒绝并说明原因"

    # ================= 5. 崩溃日志链路 =================
    with _check("崩溃日志写入") as c:
        try:
            raise RuntimeError("selftest 故意抛出的异常（用于验证崩溃记录链路）")
        except RuntimeError:
            crash = logsetup.write_crash_report(*sys.exc_info(), source="selftest")
        assert crash and crash.exists(), "崩溃日志未生成"
        text = crash.read_text(encoding="utf-8")
        assert "TRACEBACK" in text, "缺少堆栈"
        assert "selftest 故意抛出" in text, "缺少异常信息"
        assert "线程栈" in text, "缺少线程栈快照"
        assert __version__ in text, "缺少版本号"
        name = crash.name
        crash.unlink()          # 验证完就删掉，不留假崩溃日志
        c.detail = f"{name}（内容校验通过后删除）"

    # ================= 5. 汇总 =================
    passed = sum(1 for _, ok, _ in CHECK_LOG if ok)
    total = len(CHECK_LOG)
    all_ok = passed == total
    elapsed = (_dt.datetime.now() - t0).total_seconds()

    lines = [
        "=" * 74,
        f"{APP_NAME} v{__version__} —— 自检报告",
        "=" * 74,
        f"时间      : {t0:%Y-%m-%d %H:%M:%S}",
        f"运行模式  : {'打包 exe' if paths.is_frozen() else '源码运行'}",
        f"可执行文件: {sys.executable}",
        f"数据根目录: {paths.writable_root()}",
        f"系统      : {platform.system()} {platform.release()} ({platform.version()})",
        f"CPU 架构  : {platform.machine()}",
        f"Python    : {sys.version.split()[0]}",
        f"耗时      : {elapsed:.1f} 秒",
        "",
        f"结果：{'全部通过 ✓' if all_ok else f'有 {total - passed} 项失败 ✗'}   ({passed}/{total})",
        "-" * 74,
    ]
    for name, ok, detail in CHECK_LOG:
        lines.append(f"  {'✓' if ok else '✗'} {name}")
        if detail:
            lines.append(f"      {detail}")

    if not all_ok:
        lines += ["", "失败项明细：", "-" * 74]
        lines += [f"  ✗ {n}\n      {d}" for n, ok, d in CHECK_LOG if not ok]

    lines += [
        "",
        "-" * 74,
        f"自检产生的临时文件：{tmp}",
        "（可自行删除；本报告文件位于 logs 目录，可直接发给开发者）",
        "=" * 74,
    ]
    report = "\n".join(lines)

    log = logsetup.get_logger("app.selftest")
    log.info("自检完成: %d/%d 通过，用时 %.1fs", passed, total, elapsed)
    for name, ok, detail in CHECK_LOG:
        (log.info if ok else log.error)("  [%s] %s | %s",
                                        "OK" if ok else "FAIL", name, detail)

    return all_ok, report


def write_report(report: str) -> Path:
    ts = _dt.datetime.now()
    p = paths.log_dir() / f"selftest_{ts:%Y-%m-%d_%H%M%S}.txt"
    p.write_text(report, encoding="utf-8")
    return p


def main_selftest() -> int:
    """自检入口：写报告文件，并用弹窗告知结果"""
    logfile = logsetup.init_logging()
    try:
        ok, report = run_selftest()
    except Exception:
        report = ("自检运行本身发生异常：\n\n" + traceback.format_exc()
                  + f"\n\n请把本文件发送给开发者。\n运行日志：{logfile}")
        ok = False
        logsetup.write_crash_report(*sys.exc_info(), source="selftest")

    path = write_report(report)
    try:
        print(report)
    except Exception:
        pass

    # --windowed 打包后没有控制台，用弹窗把结论告知用户
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo(
            "自检结果",
            ("自检全部通过 ✓\n\n程序功能正常，可以放心使用。"
             if ok else "自检发现问题 ✗\n\n请把下面这份报告文件发送给开发者。")
            + f"\n\n报告文件：\n{path}")
        root.destroy()
    except Exception:
        pass
    return 0 if ok else 1
