# -*- coding: utf-8 -*-
"""预览窗口冒烟测试：构建 PreviewWindow，验证三页参数、实时渲染与回填

不进入 mainloop，用 update() 手动泵事件，等预览图真的画出来再断言。
"""
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from app import logsetup                       # noqa: E402
logsetup.init_logging()

failed = []


def pump(app, win, seconds, until=None):
    """手动泵事件，最多 seconds 秒；until 为真值即提前返回"""
    t0 = time.time()
    while time.time() - t0 < seconds:
        app.update()
        if until is not None and until():
            return True
        time.sleep(0.02)
    return until is None or bool(until())


def main():
    from app.gui import App, PreviewWindow

    app = App()
    app.update_idletasks()
    app.update()

    sample = ROOT / "test" / "sample.pdf"
    assert sample.exists(), sample
    app.add_paths([str(sample)])
    app.update()

    job = app.collect_job()
    print(f"  主界面 job: {job.print_params.mode} / {job.layout_params.paper} / "
          f"{job.scan_params.out_mode} / vary={job.scan_params.page_variation}")

    win = PreviewWindow(app, str(sample), job, page_index=0)
    app.update()

    # 1. 三页参数都建出来了
    n_print = len([1 for g, _, _ in win._p_sliders if g == "print"])
    n_scan = len([1 for g, _, _ in win._p_sliders if g == "scan"])
    print(f"  滑块数: print={n_print} scan={n_scan}")
    assert win._by_field.get("density") is not None, "缺打印浓度滑块"
    assert win._by_field.get("paper_base") is not None, "缺扫描纸底滑块"
    assert hasattr(win, "sl_margin"), "缺版式页边距滑块"

    # 2. 等首帧
    ok = pump(app, win, 25.0, until=lambda: win.shown_token > 0)
    print(f"  首帧: {'OK' if ok else 'TIMEOUT'} token={win.shown_token} "
          f"cost={win.lbl_cost.cget('text')!r}")
    assert ok, "首帧预览没有渲染出来"

    # 3. 改打印参数 -> 必须走 reprint 路径并出图
    t0 = win.shown_token
    win._by_field["density"].var.set(1.35)
    win._by_field["density"]._on_move()
    ok = pump(app, win, 25.0, until=lambda: win.shown_token > t0)
    print(f"  改浓度: {'OK' if ok else 'TIMEOUT'} token={win.shown_token}")
    assert ok, "改打印浓度后没有重绘"
    assert abs(win.pp.density - 1.35) < 1e-6, win.pp.density

    # 4. 切打印引擎 -> 彩色链路
    t0 = win.shown_token
    win.pmode.set("color_laser")
    win._on_pmode()
    ok = pump(app, win, 25.0, until=lambda: win.shown_token > t0)
    print(f"  切彩色引擎: {'OK' if ok else 'TIMEOUT'} token={win.shown_token}")
    assert ok, "切换打印引擎后没有重绘"
    assert win.pp.mode == "color_laser"

    # 5. 改纸张 -> 需要重建 session
    t0 = win.shown_token
    from app import paper
    win.papervar.set(paper.PAPER_LABELS["A5"])
    win._on_paper_change()
    ok = pump(app, win, 30.0, until=lambda: win.shown_token > t0)
    print(f"  换纸张A5: {'OK' if ok else 'TIMEOUT'} token={win.shown_token}")
    assert ok, "换纸张后没有重绘"
    assert win.lp.paper == "A5", win.lp.paper

    # 6. 翻页
    t0 = win.shown_token
    win._goto(1)
    ok = pump(app, win, 30.0, until=lambda: win.shown_token > t0)
    print(f"  翻到第2页: {'OK' if ok else 'TIMEOUT'} page={win.page_index}")
    assert ok and win.page_index == 1

    # 7. 应用到主界面：打印/版式参数也要回填
    win._apply_to_main()
    app.update()
    print(f"  回填后主界面: mode={app.v_mode.get()} density={app.v_density.get():.2f} "
          f"paper={app.v_paper.get()} margin={app.v_margin.get():.1f}")
    assert app.v_mode.get() == "color_laser", app.v_mode.get()
    assert abs(app.v_density.get() - 1.35) < 1e-6
    assert app.v_paper.get() == paper.PAPER_LABELS["A5"], app.v_paper.get()

    # 8. 恢复初始参数
    win._reset()
    app.update()
    print(f"  恢复后: density={win.pp.density:.2f} mode={win.pp.mode}")
    assert abs(win.pp.density - job.print_params.density) < 1e-6

    win._on_close()
    app.update()
    app.destroy()
    print("预览窗口测试通过")


try:
    main()
except Exception:
    failed.append(traceback.format_exc())
    print("预览窗口测试失败：")
    print(failed[-1])

sys.exit(1 if failed else 0)
