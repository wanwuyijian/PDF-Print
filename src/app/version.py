# -*- coding: utf-8 -*-
"""
版本与产品信息 —— 单一数据源（Single Source of Truth）

后续升级版本时，只需修改本文件中的 __version__ 与 BUILD_DATE，
GUI 标题栏、关于对话框、日志、exe 文件属性（由 build/version_info.txt 生成）均从此处派生。
"""

APP_NAME = "PDF 打印扫描模拟器"          # 显示名称（中文）
APP_NAME_EN = "PdfPrintScanSimulator"      # 英文标识（用于目录名、文件名）
APP_ID = "com.local.pdfprint.scansim"      # 应用唯一标识

__version__ = "1.0.4"
BUILD_DATE = "2026-10-16"
AUTHOR = "PdfPrint"

# ---- 语义化版本拆分（供 exe 文件属性 / 比较使用）----
VERSION_MAJOR, VERSION_MINOR, VERSION_PATCH = (int(x) for x in __version__.split("."))
VERSION_TUPLE = (VERSION_MAJOR, VERSION_MINOR, VERSION_PATCH, 0)


def version_string() -> str:
    """返回带产品名的完整版本串，例如：PDF 打印扫描模拟器 v1.0.4 (2026-10-16)"""
    return f"{APP_NAME} v{__version__} ({BUILD_DATE})"
