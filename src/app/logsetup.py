# -*- coding: utf-8 -*-
"""
日志系统

设计目标：**出问题了能靠日志定位**。因此这里做了三层记录：

1. 运行日志  logs/app_YYYY-MM-DD.log
   - RotatingFileHandler，按天切分 + 单文件 5MB 上限，保留 14 天
   - 记录 启动环境 / 每个文件的处理参数 / 每页耗时 / 异常堆栈

2. 崩溃日志  logs/crash_YYYY-MM-DD_HHMMSS.log
   - 任何**未捕获异常**（主线程、子线程、tkinter 回调）都会单独落一份完整快照
   - 快照包含：版本号、系统信息、完整 traceback、线程栈、当前处理参数
   - 崩溃日志独立成文件，用户直接把这一份发出来即可定位问题

3. 控制台输出（开发模式下）
"""

from __future__ import annotations

import datetime as _dt
import logging
import logging.handlers
import platform
import sys
import threading
import traceback
from pathlib import Path

from . import paths
from .version import APP_NAME, __version__

LOG_FORMAT = "%(asctime)s [%(levelname)-7s] [%(threadName)s] %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_initialized = False
_crash_hook_installed = False

# 崩溃时用于附加到日志里的“现场信息”，由业务代码按需写入
CRASH_CONTEXT: dict[str, str] = {}

# 崩溃回调（GUI 注册后可在崩溃时弹窗提示日志位置）
_crash_listeners: list = []


def register_crash_listener(fn) -> None:
    """注册崩溃回调：fn(log_path: Path, exc_text: str)"""
    _crash_listeners.append(fn)


def set_context(key: str, value: str) -> None:
    """写入崩溃快照的上下文（例如当前文件、当前参数）"""
    CRASH_CONTEXT[key] = str(value)


def clear_context(*keys: str) -> None:
    if not keys:
        CRASH_CONTEXT.clear()
    for k in keys:
        CRASH_CONTEXT.pop(k, None)


# --------------------------------------------------------------------------- #
# 初始化
# --------------------------------------------------------------------------- #
def init_logging(level: int = logging.INFO) -> Path:
    """初始化日志系统，返回当前运行日志文件路径。重复调用安全。"""
    global _initialized
    log_file = paths.log_dir() / f"app_{_dt.date.today():%Y-%m-%d}.log"
    if _initialized:
        return log_file

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    fmt = logging.Formatter(LOG_FORMAT, DATE_FORMAT)

    # 1) 滚动文件日志
    fh = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    fh.setLevel(level)
    fh.setFormatter(fmt)
    root.addHandler(fh)

    # 2) 控制台（源码运行时方便调试；noconsole 打包后 stderr 可能为 None）
    if sys.stderr is not None:
        try:
            ch = logging.StreamHandler(sys.stderr)
            ch.setLevel(logging.DEBUG if not paths.is_frozen() else logging.WARNING)
            ch.setFormatter(fmt)
            root.addHandler(ch)
        except Exception:
            pass

    # 降噪：第三方库只记警告
    for noisy in ("PIL", "fitz", "pymupdf", "numpy", "matplotlib", "fontTools"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _initialized = True

    log = logging.getLogger("app.boot")
    log.info("=" * 78)
    log.info("%s v%s 启动", APP_NAME, __version__)
    log.info("运行模式: %s", "打包 exe" if paths.is_frozen() else "源码")
    log.info("可执行文件: %s", sys.executable)
    log.info("数据根目录: %s", paths.writable_root())
    log.info("日志文件: %s", log_file)
    log.info("系统: %s %s (%s)", platform.system(), platform.release(), platform.version())
    log.info("Python: %s", sys.version.replace("\n", " "))
    log.info("=" * 78)

    install_crash_hooks()
    return log_file


# --------------------------------------------------------------------------- #
# 崩溃捕获
# --------------------------------------------------------------------------- #
def _system_snapshot() -> str:
    lines = [
        "-" * 78,
        "崩溃快照 / CRASH SNAPSHOT",
        "-" * 78,
        f"时间        : {_dt.datetime.now():%Y-%m-%d %H:%M:%S}",
        f"程序        : {APP_NAME} v{__version__}",
        f"运行模式    : {'打包 exe' if paths.is_frozen() else '源码'}",
        f"可执行文件  : {sys.executable}",
        f"工作目录    : {paths.app_dir()}",
        f"数据根目录  : {paths.writable_root()}",
        f"操作系统    : {platform.system()} {platform.release()} ({platform.version()})",
        f"CPU 架构    : {platform.machine()}",
        f"Python      : {sys.version.replace(chr(10), ' ')}",
    ]
    if CRASH_CONTEXT:
        lines.append("现场上下文  :")
        for k, v in CRASH_CONTEXT.items():
            lines.append(f"    {k} = {v}")
    return "\n".join(lines)


def _dump_threads() -> str:
    """把所有线程的调用栈打进快照——多线程处理时定位卡死/异常点非常有用。"""
    out = ["", "线程栈 / THREAD DUMP", "-" * 78]
    frames = sys._current_frames()
    for t in threading.enumerate():
        frame = frames.get(t.ident)
        out.append(f"\n>>> 线程 {t.name} (ident={t.ident}, daemon={t.daemon}, alive={t.is_alive()})")
        if frame is None:
            out.append("    (无可用栈帧)")
            continue
        out.extend("    " + line for line in traceback.format_stack(frame))
    return "\n".join(out)


def write_crash_report(exc_type, exc_value, exc_tb, source: str = "unknown") -> Path | None:
    """把一次未捕获异常写成独立的崩溃日志文件，返回文件路径。"""
    try:
        ts = _dt.datetime.now()
        path = paths.log_dir() / f"crash_{ts:%Y-%m-%d_%H%M%S}.log"

        tb_text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        content = [
            _system_snapshot(),
            f"异常来源    : {source}",
            "",
            "异常 / TRACEBACK",
            "-" * 78,
            tb_text.rstrip(),
            _dump_threads(),
            "",
            "-" * 78,
            "提示：请把本文件完整发送给开发者用于定位问题。",
            "-" * 78,
        ]
        path.write_text("\n".join(content), encoding="utf-8")

        logging.getLogger("app.crash").critical(
            "捕获到未处理异常（来源=%s），已写入崩溃日志: %s\n%s", source, path, tb_text
        )

        for fn in list(_crash_listeners):
            try:
                fn(path, tb_text)
            except Exception:
                pass
        return path
    except Exception:
        # 崩溃记录器自身绝不能再抛异常
        try:
            logging.getLogger("app.crash").exception("写崩溃日志失败")
        except Exception:
            pass
        return None


def install_crash_hooks() -> None:
    """挂载 主线程 / 子线程 / tkinter 三类异常钩子。"""
    global _crash_hook_installed
    if _crash_hook_installed:
        return

    original_hook = sys.excepthook

    def _sys_hook(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            original_hook(exc_type, exc_value, exc_tb)
            return
        write_crash_report(exc_type, exc_value, exc_tb, source="main-thread")

    sys.excepthook = _sys_hook

    def _thread_hook(args):
        if issubclass(args.exc_type, SystemExit):
            return
        write_crash_report(
            args.exc_type, args.exc_value, args.exc_traceback,
            source=f"thread:{args.thread.name if args.thread else '?'}",
        )

    threading.excepthook = _thread_hook
    _crash_hook_installed = True


def install_tk_crash_hook(root) -> None:
    """tkinter 的异常走 report_callback_exception，必须单独接管，否则会被吞掉。"""
    def _tk_hook(exc_type, exc_value, exc_tb):
        write_crash_report(exc_type, exc_value, exc_tb, source="tkinter-callback")

    root.report_callback_exception = _tk_hook


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def log_exception(logger: logging.Logger, msg: str) -> None:
    """记录异常并附带堆栈（用于可预期的失败，如某个 PDF 打不开）"""
    logger.error("%s\n%s", msg, traceback.format_exc())
