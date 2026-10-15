# -*- coding: utf-8 -*-
"""
PDF 打印扫描模拟器 —— 程序入口

打包命令见 build/build_exe.bat，或直接运行 tools/build.py。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _setup_path() -> None:
    """源码运行时把 src 加入模块搜索路径；打包后由 PyInstaller 处理"""
    if getattr(sys, "frozen", False):
        return
    here = Path(__file__).resolve().parent
    if str(here) not in sys.path:
        sys.path.insert(0, str(here))


def _setup_dpi() -> None:
    """Windows 高分屏：开启 DPI 感知，避免界面模糊/错位"""
    if not sys.platform.startswith("win"):
        return
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)   # Win8.1+
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()        # Win7/8
    except Exception:
        pass


def main() -> int:
    _setup_path()
    _setup_dpi()

    # noconsole 打包后 sys.stdout/stderr 可能是 None，任何 print 都会炸，直接兜住
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")

    # 自检模式：跑一遍完整链路并输出报告，用于快速定位环境/代码问题
    if "--selftest" in sys.argv or "-t" in sys.argv:
        from app.selftest import main_selftest
        return main_selftest()

    from app.gui import main as gui_main
    gui_main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
