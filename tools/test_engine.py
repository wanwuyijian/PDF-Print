# -*- coding: utf-8 -*-
"""引擎冒烟测试：跑通两种模式，输出对比图供人工核对"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from app import engine, logsetup                       # noqa: E402
from app.config import (RenderJob, PrintParams, ScanParams,   # noqa: E402
                        LayoutParams, OutputParams)

logsetup.init_logging()

SRC = ROOT / "test" / "sample.pdf"
OUT = ROOT / "test" / "out"
OUT.mkdir(parents=True, exist_ok=True)


def run(tag, job, pages="all", out_format="png", dpi=200):
    job.pages = pages
    job.output_params.out_format = out_format
    job.output_params.render_dpi = dpi
    job.output_params.out_dpi = dpi
    job.output_params.out_dir = str(OUT)
    job.output_params.name_suffix = f"_{tag}"
    t = time.perf_counter()
    r = engine.process_pdf(SRC, job, out_dir=OUT,
                           progress=lambda d, t_, m: print(f"  [{d}/{t_}] {m}"))
    print(f"[{tag}] ok={r.ok} pages={r.pages_done} {time.perf_counter()-t:.2f}s err={r.error}")
    for o in r.outputs:
        print("   ->", Path(o).name)
    return r


print("=" * 60, "\n1) 黑白激光 → 黑白扫描")
run("mono", RenderJob(pages="1",
                      print_params=PrintParams(mode="mono_laser"),
                      scan_params=ScanParams(),
                      layout_params=LayoutParams(paper="A4", margin_mm=8)))

print("=" * 60, "\n2) 彩色激光 → 黑白扫描")
run("colorlaser", RenderJob(pages="2",
                            print_params=PrintParams(mode="color_laser"),
                            scan_params=ScanParams(),
                            layout_params=LayoutParams(paper="A4", margin_mm=8)))

print("=" * 60, "\n3) 彩色喷墨 → 黑白扫描")
run("colorink", RenderJob(pages="2",
                          print_params=PrintParams(mode="color_inkjet"),
                          scan_params=ScanParams(),
                          layout_params=LayoutParams(paper="A4", margin_mm=8)))

print("=" * 60, "\n4) A5 纸张 + 二值扫描 + 300dpi")
run("a5binary", RenderJob(pages="1",
                          print_params=PrintParams(mode="mono_laser"),
                          scan_params=ScanParams(out_mode="binary"),
                          layout_params=LayoutParams(paper="A5", margin_mm=6),
                          ),
    out_format="pdf", dpi=300)

print("=" * 60, "\n5) 输出 PDF（默认）")
r = run("pdfout", RenderJob(pages="all",
                            print_params=PrintParams(mode="mono_laser"),
                            scan_params=ScanParams(),
                            layout_params=LayoutParams(paper="A4")),
        out_format="pdf")
print("done")
