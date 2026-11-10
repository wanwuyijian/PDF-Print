# -*- coding: utf-8 -*-
"""把主界面 + 预览窗口截图保存，便于检查排版"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from app import logsetup                       # noqa: E402
logsetup.init_logging()

# 关键：开启 DPI 感知后再建窗口，否则 tkinter 用逻辑坐标而 ImageGrab 抓物理像素，
# 在高分屏（如 1920x1080 @125%）上只会截到窗口的左上部。
from main import _setup_dpi                    # noqa: E402
_setup_dpi()

from app.gui import App, PreviewWindow         # noqa: E402
from app.config import (LayoutParams, OutputParams, PrintParams,   # noqa: E402
                        RenderJob, ScanParams)
from PIL import ImageGrab                      # noqa: E402

out = ROOT / "test" / "ui"
out.mkdir(parents=True, exist_ok=True)


def pump(obj, seconds):
    end = time.perf_counter() + seconds
    while time.perf_counter() < end:
        obj.update()
        time.sleep(0.01)


def grab(win, name):
    win.update_idletasks()
    win.deiconify()
    win.lift()
    try:
        win.attributes("-topmost", True)
    except Exception:
        pass
    win.focus_force()
    pump(win, 0.8)
    x, y = win.winfo_rootx(), win.winfo_rooty()
    w, h = win.winfo_width(), win.winfo_height()
    img = ImageGrab.grab(bbox=(x, y, x + w, y + h))
    img.save(out / name)
    print(f"  {name}  {img.size}  (窗口 {w}x{h})")


app = App()
app.geometry("1320x820+40+20")
app.update()

for f in sorted((ROOT / "test").glob("*.pdf")):
    n = 3
    app.tree.insert("", "end", iid=str(f), values=(f.name, n, "12.3 KB"))
    app.files.append(f)
    app.page_counts[str(f)] = n
app._refresh_count()
# 截图会进 docs 并公开在仓库里，输出目录必须换成中性占位路径，
# 不能把本机的真实目录结构截进去，否则等于公开自己的磁盘布局。
app.v_outdir.set(r"D:\扫描输出")
app.v_seed.set("20261109")
pump(app, 0.4)

for i, name in enumerate(["ui_tab1_print.png", "ui_tab2_scan.png", "ui_tab3_layout.png"]):
    app.nb.select(i)
    grab(app, name)

# 预览窗口：三个参数页各截一张（v1.0.5 起预览可调打印/扫描/版式）
job = RenderJob(print_params=PrintParams(mode="mono_laser"),
                scan_params=ScanParams(),
                layout_params=LayoutParams(paper="A4"),
                output_params=OutputParams(render_dpi=200, out_dpi=200))
win = PreviewWindow(app, sorted((ROOT / "test").glob("*.pdf"))[0], job, 0)
win.geometry("1330x930+60+30")
pump(app, 2.5)

for i, name in enumerate(["ui_preview_print.png", "ui_preview_scan.png",
                          "ui_preview_layout.png"]):
    win.nb.select(i)
    pump(app, 0.5)
    grab(win, name)

win._on_close()
app.destroy()
print("OK")
