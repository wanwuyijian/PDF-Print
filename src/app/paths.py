# -*- coding: utf-8 -*-
"""
运行路径解析

打包成单文件 exe 后，程序运行时会把自身解压到一个临时目录（sys._MEIPASS），
而 *用户数据*（日志、预设、输出）必须落在 exe 所在的目录旁边，而不是临时目录，
否则 PyInstaller 退出时会连日志一起删掉。

优先级：
    exe 同目录（可写时）  ->  否则回落到 %LOCALAPPDATA%\\PdfPrintScanSimulator
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from .version import APP_NAME_EN

_WRITABLE_ROOT_CACHE: Path | None = None


def is_frozen() -> bool:
    """是否运行在 PyInstaller 打包后的 exe 中"""
    return bool(getattr(sys, "frozen", False))


def app_dir() -> Path:
    """程序所在目录（exe 同目录 / 源码目录）"""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    # src/app/paths.py -> 上溯三级得到项目根目录
    return Path(__file__).resolve().parent.parent.parent


def resource_path(*parts: str) -> Path:
    """读取随程序打包的只读资源（图标等）"""
    if is_frozen():
        base = Path(getattr(sys, "_MEIPASS", app_dir()))
    else:
        base = app_dir()
    return base.joinpath(*parts)


def _fallback_dir() -> Path:
    local = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or str(Path.home())
    return Path(local) / APP_NAME_EN


def writable_root() -> Path:
    """
    返回可写的程序数据根目录。
    优先 exe 同目录（绿色版习惯，方便用户找日志），失败则回落用户目录。
    """
    global _WRITABLE_ROOT_CACHE
    if _WRITABLE_ROOT_CACHE is not None:
        return _WRITABLE_ROOT_CACHE

    candidate = app_dir()
    try:
        candidate.mkdir(parents=True, exist_ok=True)
        probe = candidate / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        _WRITABLE_ROOT_CACHE = candidate
    except Exception:
        fb = _fallback_dir()
        fb.mkdir(parents=True, exist_ok=True)
        _WRITABLE_ROOT_CACHE = fb
    return _WRITABLE_ROOT_CACHE


def log_dir() -> Path:
    d = writable_root() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def config_dir() -> Path:
    d = writable_root() / "config"
    d.mkdir(parents=True, exist_ok=True)
    return d


def default_output_dir() -> Path:
    d = writable_root() / "output"
    d.mkdir(parents=True, exist_ok=True)
    return d


# --------------------------------------------------------------------------- #
# 可移植性诊断
# --------------------------------------------------------------------------- #
def is_portable() -> bool:
    """数据目录是否就落在程序旁边（绿色模式）"""
    return writable_root() == app_dir()


def data_root_note() -> str:
    """一句话说明数据/日志落在哪，方便用户找日志"""
    root = writable_root()
    if is_portable():
        return f"数据与日志目录：{root}（程序所在目录）"
    return (f"数据与日志目录：{root}\n"
            f"（程序所在目录不可写，已自动改用用户目录；"
            f"如需放回程序旁边，请把程序移到有写入权限的位置）")


def writable_root_status() -> tuple[bool, str]:
    """
    检查数据根目录是否真的能写，并返回原因。
    用于自检：跨机部署时最常见的问题就是"日志目录不可写"。
    """
    root = writable_root()
    try:
        root.mkdir(parents=True, exist_ok=True)
        probe = root / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True, f"可写：{root}"
    except Exception as e:
        return False, f"不可写：{root}（{type(e).__name__}: {e}）"


def temp_dir_status() -> tuple[bool, str]:
    """
    检查系统临时目录可用性。

    打包成单文件 exe 后，PyInstaller 启动时会把整个运行环境解压到
    临时目录（约 200MB）。如果临时目录不可用，程序会在 Python 代码
    还没跑起来之前就失败，用户只能看到一个没有信息量的报错。
    这里提前探测，至少能在日志与自检里说清楚。
    """
    import shutil
    import tempfile
    tmp = Path(tempfile.gettempdir())
    try:
        tmp.mkdir(parents=True, exist_ok=True)
        usage = shutil.disk_usage(tmp)
        free_gb = usage.free / 1024 ** 3
        if free_gb < 0.5:
            return False, (f"临时目录 {tmp} 剩余空间仅 {free_gb:.2f} GB，"
                           f"不足以解压运行环境（需约 0.5 GB）")
        probe = tmp / ".pdfprint_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True, f"可写：{tmp}（剩余 {free_gb:.1f} GB）"
    except Exception as e:
        return False, f"临时目录不可用：{tmp}（{type(e).__name__}: {e}）"
