# -*- coding: utf-8 -*-
"""GUI 构建冒烟测试：不进入 mainloop，仅构建/销毁界面并自检控件是否齐全"""
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from app import logsetup                       # noqa: E402
logsetup.init_logging()

failed = []
try:
    from app.gui import App                    # noqa: E402

    app = App()
    app.update_idletasks()
    app.update()

    # 检查关键控件存在
    checks = {
        "文件列表": hasattr(app, "tree"),
        "进度条": hasattr(app, "pb"),
        "开始按钮": hasattr(app, "btn_run"),
        "取值范围": app.v_pages.get(),
        "纸张下拉项": len(app.cb_paper["values"]),
        "滑块数量": len(app._sliders),
    }
    for k, v in checks.items():
        print(f"  {k}: {v}")

    # 逐个应用所有内置预设，检查参数映射是否报错
    from app.config import PRESET_ORDER
    for name in PRESET_ORDER:
        app.v_preset.set(name)
        app._apply_preset()
        job = app.collect_job()
        print(f"  预设[{name}] -> {job.print_params.mode} / 边距{job.layout_params.margin_mm} "
              f"/ {job.scan_params.out_mode}")

    # 各打印模式切换
    for m in ("mono_laser", "color_laser", "color_inkjet"):
        app.v_mode.set(m)
        job = app.collect_job()
        assert job.print_params.mode == m

    # 各纸张尺寸
    from app import paper
    for k in paper.PAPER_ORDER:
        app.v_paper.set(paper.PAPER_LABELS[k])
        app._on_paper_change()
        job = app.collect_job()
        assert job.layout_params.paper == k, (k, job.layout_params.paper)
    print("  纸张尺寸映射: 全部 OK")

    # 输出格式
    from app.config import OUTPUT_FORMATS
    for k, v in OUTPUT_FORMATS.items():
        app.v_format.set(v)
        app._sync_format()
        assert app.collect_job().output_params.out_format == k, (k, app._format_key)
    print("  输出格式映射: 全部 OK")

    app.destroy()
    print("GUI 构建测试通过")
except Exception:
    failed.append(traceback.format_exc())
    print("GUI 构建测试失败：")
    print(failed[-1])

sys.exit(1 if failed else 0)
