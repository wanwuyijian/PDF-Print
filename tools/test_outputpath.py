# -*- coding: utf-8 -*-
"""
输出路径功能端到端测试（v1.0.3 新增）

验证「输出目录 + 输出文件名」能组合出用户想要的完整路径，
以及批量自动加序号、同名文件不被覆盖。
"""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from app import engine, logsetup                        # noqa: E402
from app.config import (LayoutParams, OutputParams,      # noqa: E402
                        PrintParams, RenderJob, ScanParams)

logsetup.init_logging()
SRC = ROOT / "test" / "sample.pdf"

FAILED: list[str] = []


def check(name, cond, detail=""):
    print(f"  {'✓' if cond else '✗'} {name}{('  ' + detail) if detail else ''}")
    if not cond:
        FAILED.append(name)


def job(fmt="pdf", out_dir="", out_name=""):
    return RenderJob(PrintParams(mode="mono_laser"), ScanParams(),
                     LayoutParams(paper="A5", margin_mm=6),
                     OutputParams(render_dpi=72, out_dpi=72, out_format=fmt,
                                  out_dir=str(out_dir), out_name=out_name, seed=1))


tmp = Path(tempfile.mkdtemp(prefix="pdfprint_outtest_"))

print("=" * 70)
print("① 指定输出文件名 → 文件名精确生效")
print("=" * 70)
r = engine.process_pdf(SRC, job(out_dir=tmp, out_name="我的合同扫描件"),
                       file_count=1)
check("处理成功", r.ok, r.error or "")
check("落在指定文件名上", (tmp / "我的合同扫描件.pdf").exists(),
      f"实际产出: {[Path(p).name for p in r.outputs]}")
check("只有 1 个输出文件", len(r.outputs) == 1, f"实际 {len(r.outputs)} 个")

print()
print("=" * 70)
print("② 未指定文件名 → 沿用源文件名自动命名（向后兼容）")
print("=" * 70)
d2 = tmp / "auto"
r = engine.process_pdf(SRC, job(out_dir=d2), file_count=1)
check("按源文件名 + 后缀生成", (d2 / "sample_scan.pdf").exists(),
      f"实际产出: {[Path(p).name for p in r.outputs]}")

print()
print("=" * 70)
print("③ 批量多文件 + 指定文件名 → 自动加序号，不互相覆盖")
print("=" * 70)
d3 = tmp / "batch"
outs = []
for i in range(3):
    r = engine.process_pdf(SRC, job(out_dir=d3, out_name="批量结果"),
                           file_index=i, file_count=3)
    check(f"第 {i + 1} 个文件处理成功", r.ok, r.error or "")
    outs += [Path(p).name for p in r.outputs]
names = sorted(p.name for p in d3.glob("*.pdf"))
check("产出 3 个不同文件", len(names) == 3, f"实际 {names}")
check("命名为 批量结果_1/2/3.pdf",
      names == ["批量结果_1.pdf", "批量结果_2.pdf", "批量结果_3.pdf"], f"实际 {names}")

print()
print("=" * 70)
print("④ 同名文件不被覆盖（连跑两次）")
print("=" * 70)
d4 = tmp / "noclobber"
engine.process_pdf(SRC, job(out_dir=d4, out_name="重复"), file_count=1)
first = d4 / "重复.pdf"
keep = first.read_bytes()
engine.process_pdf(SRC, job(out_dir=d4, out_name="重复"), file_count=1)
after = sorted(p.name for p in d4.glob("*.pdf"))
check("第二次没有覆盖原文件", first.read_bytes() == keep, f"目录内容 {after}")
check("第二次另存为带序号的文件", "重复_1.pdf" in after, f"实际 {after}")

print()
print("=" * 70)
print("⑤ 各种输出格式都遵循同一套路径规则")
print("=" * 70)
for fmt, ext in (("pdf", ".pdf"), ("png", ".png"), ("jpg", ".jpg")):
    d = tmp / f"fmt_{fmt}"
    r = engine.process_pdf(SRC, job(fmt=fmt, out_dir=d, out_name=f"格式测试_{fmt}"),
                           file_count=1)
    made = sorted(p.name for p in d.glob(f"*{ext}"))
    check(f"{fmt} 输出到指定基名", bool(made), f"实际 {made[:2]}")

d6 = tmp / "mix"
r = engine.process_pdf(SRC, job(fmt="pdf_img", out_dir=d6, out_name="混合输出"),
                       file_count=1)
made = sorted(p.name for p in d6.iterdir())
check("pdf_img 同时产出 PDF 与图片",
      any(n.endswith(".pdf") for n in made) and any(n.endswith(".jpg") for n in made),
      f"实际 {made}")

print()
print("=" * 70)
print("⑥ 用户误把完整路径粘进「输出文件名」→ 取最后一段，不报错")
print("=" * 70)
d7 = tmp / "paste"
r = engine.process_pdf(SRC, job(out_dir=d7, out_name=r"D:\某个\路径\粘贴过来的名字"),
                       file_count=1)
check("仍落在输出目录下", (d7 / "粘贴过来的名字.pdf").exists(),
      f"实际产出: {[Path(p).name for p in r.outputs]}")

print()
print("=" * 70)
print(f"结果：{'全部通过 ✓' if not FAILED else f'{len(FAILED)} 项失败 ✗'}")
print("=" * 70)
print(f"（测试产物在 {tmp}，可自行删除）")
sys.exit(1 if FAILED else 0)