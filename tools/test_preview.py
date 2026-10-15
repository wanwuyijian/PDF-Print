# -*- coding: utf-8 -*-
"""预览窗口实时调参测试：验证三个核心滑块能即时改变画面，并测量响应时间"""
import sys
import time
from pathlib import Path
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from app import logsetup                       # noqa: E402
logsetup.init_logging()

from app.gui import App, PreviewWindow         # noqa: E402
from app.config import RenderJob, LayoutParams, PrintParams, ScanParams  # noqa: E402

OUT = ROOT / "test" / "ui"
OUT.mkdir(parents=True, exist_ok=True)
SRC = ROOT / "test" / "sample.pdf"


def pump(obj, seconds: float):
    """空转事件循环，等待后台线程回传"""
    end = time.perf_counter() + seconds
    while time.perf_counter() < end:
        obj.update()
        time.sleep(0.01)


def main():
    app = App()
    app.geometry("1180x780+40+20")
    app.update()

    job = RenderJob(print_params=PrintParams(mode="mono_laser"),
                    scan_params=ScanParams(),
                    layout_params=LayoutParams(paper="A4", margin_mm=8))

    t0 = time.perf_counter()
    win = PreviewWindow(app, SRC, job, page_index=0)
    pump(app, 1.2)
    print(f"会话构建完成 {time.perf_counter() - t0:.2f}s, session={'OK' if win.session else 'NULL'}")
    assert win.session is not None, "预览会话未构建成功"
    print("  预览尺寸:", win.session.base_gray.shape, win.session.info)
    print("  信息栏:", win.lbl_info.cget("text").replace("\n", " | "))

    # --- 测试三个核心滑块 ---
    cases = [
        ("blur", 1.60, "大模糊"),
        ("blur", 0.10, "低模糊"),
        ("noise", 0.95, "高噪点"),
        ("noise", 0.05, "低噪点"),
    ]
    for field, value, label in cases:
        win._p_sliders[field].var.set(value)
        t0 = time.perf_counter()
        win._on_change(field)
        win._cancel_timers()
        win._request_render(quick=False)
        time.sleep(0.02)
        # 等结果
        deadline = time.perf_counter() + 6
        while time.perf_counter() < deadline:
            app.update()
            if win.last_image is not None and win.shown_token == win.token:
                break
            time.sleep(0.01)
        print(f"  {label:8s} {field}={value:.2f} -> 渲染 {win.last_cost * 1000:.0f} ms,"
              f" 端到端 {time.perf_counter() - t0:.2f}s")
        if label == "大模糊":
            Image_ = Image
            Image_.fromarray(win.last_image, mode="L").save(OUT / "prev_blur_hi.png")
        if label == "高噪点":
            Image_ = Image
            Image_.fromarray(win.last_image, mode="L").save(OUT / "prev_noise_hi.png")

    # --- 倾斜滑块 ---
    win.skew_random.set(False)
    win._on_skew_toggle()
    win._p_sliders["skew_deg"].var.set(1.60)
    win._on_change("skew_deg")
    win._cancel_timers()
    win._request_render(quick=False)
    deadline = time.perf_counter() + 6
    while time.perf_counter() < deadline:
        app.update()
        if win.shown_token == win.token:
            break
        time.sleep(0.01)
    print(f"  倾斜 1.60° -> {win.last_cost * 1000:.0f} ms")
    Image_ = Image
    Image_.fromarray(win.last_image, mode="L").save(OUT / "prev_skew_hi.png")

    # --- 截图 ---
    win.geometry("1240x900+20+10")
    pump(app, 0.8)
    try:
        from PIL import ImageGrab
        x, y = win.winfo_rootx(), win.winfo_rooty()
        w, h = win.winfo_width(), win.winfo_height()
        ImageGrab.grab(bbox=(x, y, x + w, y + h)).save(OUT / "ui_preview.png")
        print("  截图已保存 ui_preview.png", (w, h))
    except Exception as e:
        print("  截图失败:", e)

    # --- 参数回填主界面 ---
    win._apply_to_main = lambda: None       # 跳过弹窗
    win._read_sliders()
    app.v_blur.set(win.sp.blur)
    app.v_noise.set(win.sp.noise)
    print(f"  回填检查: blur={app.v_blur.get():.2f} noise={app.v_noise.get():.2f}")

    win._on_close()
    app.destroy()
    print("预览窗口测试通过")


main()
