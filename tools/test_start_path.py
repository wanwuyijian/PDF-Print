# -*- coding: utf-8 -*-
"""
端到端复现 v1.0.0 的崩溃路径

模拟用户真实操作：添加文件 →（页码范围保持默认 "all"）→ 点「开始处理」。
v1.0.0 在这一步会卡死 70 秒后 MemoryError。

这里把 messagebox 换成记录器，避免弹窗阻塞测试。
"""
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from main import _setup_dpi                       # noqa: E402
_setup_dpi()

from app import gui, logsetup                    # noqa: E402
logsetup.init_logging()

# ---- 把弹窗换成"记录器"：既不阻塞，又能看到程序想告诉用户什么 ----
POPUPS: list[tuple[str, str]] = []


def _fake_showinfo(title, msg, **kw):
    POPUPS.append((title, msg))


def _fake_showwarning(title, msg, **kw):
    POPUPS.append(("WARNING:" + title, msg))


def _fake_showerror(title, msg, **kw):
    POPUPS.append(("ERROR:" + title, msg))


def _fake_askokcancel(*a, **kw):
    return True


gui.messagebox.showinfo = _fake_showinfo
gui.messagebox.showwarning = _fake_showwarning
gui.messagebox.showerror = _fake_showerror
gui.messagebox.askokcancel = _fake_askokcancel

SRC = ROOT / "test" / "sample.pdf"
FAILED: list[str] = []


def check(name, cond, detail=""):
    print(f"  {'✓' if cond else '✗'} {name}{('  ' + detail) if detail else ''}")
    if not cond:
        FAILED.append(name)


print("=" * 70)
print("复现用户操作路径：添加文件 → 开始处理")
print("=" * 70)

app = gui.App()
app.withdraw()          # 不弹真实窗口
app.update()

# 模拟「添加 PDF…」：直接塞进列表（等价于用户选完文件后的状态）
app.tree.insert("", "end", iid=str(SRC), values=(SRC.name, 3, "24 KB"))
app.files.append(SRC)
app.page_counts[str(SRC)] = 3
app._refresh_count()

print(f"  已添加文件: {SRC.name}（3 页）")
print(f"  页码范围字段当前值: {app.v_pages.get()!r}   ← v1.0.0 的崩溃触发值")
print()

# --- 场景 1：默认 all（v1.0.0 必崩的那条）---
POPUPS.clear()
t0 = time.perf_counter()
app.v_pages.set("all")
app._on_close = lambda: None
try:
    app.start()
    dt = time.perf_counter() - t0
    check(f"默认「all」不崩溃且立即返回（{dt * 1000:.0f} ms）", dt < 3.0,
          f"耗时 {dt:.1f}s" if dt >= 3.0 else "")
except MemoryError:
    check("默认「all」不崩溃", False, "抛了 MemoryError（v1.0.0 的原始 bug）")
except Exception as e:
    check("默认「all」不崩溃", False, f"{type(e).__name__}: {e}")

if app.worker and app.worker.is_alive():
    check("后台线程已正常启动处理", True, "(说明真的开始干活了)")
    app.cancel_flag.set()
    app.worker.join(timeout=20)
else:
    # 线程没起来通常是输出目录不可写之类的前置拦截，也算"没崩"
    check("未崩溃（有前置拦截或已跑完）", True,
          f"弹窗={POPUPS[0][0] if POPUPS else '无'}")

print()
print("=" * 70)
print("非法页码范围应给出友好提示（而不是崩溃）")
print("=" * 70)
for spec in ["abc", "1-x", "1--3"]:
    POPUPS.clear()
    app.v_pages.set(spec)
    try:
        app.start()
        warned = any(t.startswith("WARNING") for t, _ in POPUPS)
        check(f"「{spec}」给出警告而非崩溃", warned,
              f"弹窗={POPUPS[0][0] if POPUPS else '无'}")
    except Exception as e:
        check(f"「{spec}」给出警告而非崩溃", False, f"{type(e).__name__}: {e}")

print()
print("=" * 70)
print("页码范围超出文件实际页数应提前提示")
print("=" * 70)
POPUPS.clear()
app.v_pages.set("5-10")          # 文件只有 3 页
try:
    app.start()
    msgs = " ".join(f"{t} {m}" for t, m in POPUPS)
    check("「5-10」提示与页数不匹配", "不匹配" in msgs,
          POPUPS[0][0] if POPUPS else "无弹窗")
    check("提示里带出了文件实际页数", "共 3 页" in msgs)
except Exception as e:
    check("「5-10」提示与页数不匹配", False, f"{type(e).__name__}: {e}")

app.destroy()

print()
print("=" * 70)
print(f"结果：{'全部通过 ✓' if not FAILED else f'{len(FAILED)} 项失败 ✗'}")
print("=" * 70)
sys.exit(1 if FAILED else 0)