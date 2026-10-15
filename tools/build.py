# -*- coding: utf-8 -*-
"""
打包脚本：生成 Windows 版本资源 + 调用 PyInstaller 打成单文件 exe

用法：
    python tools/build.py            # 正式构建
    python tools/build.py --debug    # 保留控制台窗口，方便看报错
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
BUILD = ROOT / "build"
DIST = ROOT / "dist"
ASSETS = ROOT / "assets"

sys.path.insert(0, str(SRC))
from app.version import (APP_NAME, APP_NAME_EN, APP_ID, AUTHOR, BUILD_DATE,  # noqa: E402
                         VERSION_TUPLE, __version__)

EXE_STEM = f"{APP_NAME}_v{__version__}"


# --------------------------------------------------------------------------- #
# Windows 版本资源
# --------------------------------------------------------------------------- #
def write_version_info() -> Path:
    """生成 PyInstaller 的 version_info 文件（UTF-8 BOM，含中文产品名）"""
    v = VERSION_TUPLE
    vd = f"{v[0]}.{v[1]}.{v[2]}.{v[3]}"
    content = f'''# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=({v[0]}, {v[1]}, {v[2]}, {v[3]}),
    prodvers=({v[0]}, {v[1]}, {v[2]}, {v[3]}),
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(
        '080404b0',
        [StringStruct('CompanyName', '{AUTHOR}'),
         StringStruct('FileDescription', '{APP_NAME}'),
         StringStruct('FileVersion', '{vd}'),
         StringStruct('InternalName', '{APP_NAME_EN}'),
         StringStruct('LegalCopyright', 'Copyright (C) {AUTHOR}'),
         StringStruct('OriginalFilename', '{EXE_STEM}.exe'),
         StringStruct('ProductName', '{APP_NAME}'),
         StringStruct('ProductVersion', '{vd}'),
         StringStruct('Comments', 'Build {BUILD_DATE}')])
    ]),
    VarFileInfo([VarStruct('Translation', [2052, 1200])])
  ]
)
'''
    path = BUILD / "version_info.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8-sig")
    print(f"[1/4] 版本资源已生成: {path.name}  (v{vd})")
    return path


# --------------------------------------------------------------------------- #
# 构建
# --------------------------------------------------------------------------- #
def build(debug: bool = False) -> int:
    t0 = time.perf_counter()
    version_file = write_version_info()

    icon = ASSETS / "icon.ico"
    if not icon.exists():
        print("     图标不存在，先执行 tools/make_icon.py 生成")
        return 2

    name_ascii = f"{APP_NAME_EN}_{__version__}"
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--onefile",
        ("--console" if debug else "--windowed"),
        "--name", name_ascii,
        "--icon", str(icon),
        "--version-file", str(version_file),
        "--paths", str(SRC),
        "--distpath", str(DIST),
        "--workpath", str(BUILD / "temp"),
        "--specpath", str(BUILD),
        # 精简体积：明确排除用不到的重型依赖
        "--exclude-module", "matplotlib",
        "--exclude-module", "scipy",
        "--exclude-module", "pandas",
        "--exclude-module", "IPython",
        "--exclude-module", "pytest",
        "--exclude-module", "setuptools",
        "--exclude-module", "pip",
        "--exclude-module", "PyQt5",
        "--exclude-module", "PySide2",
        "--exclude-module", "PySide6",
        "--exclude-module", "wx",
        "--exclude-module", "notebook",
        "--noupx",
        str(SRC / "main.py"),
    ]
    print("[2/4] 调用 PyInstaller …")
    print("      " + " ".join(cmd[:8]) + " …")
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(cmd, cwd=str(ROOT), env=env)
    if proc.returncode != 0:
        print("      PyInstaller 构建失败")
        return proc.returncode

    built = DIST / f"{name_ascii}.exe"
    if not built.exists():
        print(f"      未找到产物 {built}")
        return 3

    # 改成中文名（PyInstaller 命令行在部分终端下处理中文名不稳，故构建后再改名）
    final = DIST / f"{EXE_STEM}.exe"
    if final.exists():
        final.unlink()
    shutil.copy2(built, final)
    print(f"[3/4] 产物: {final}  ({final.stat().st_size / 1048576:.1f} MB)")

    # 附带一份 README / CHANGELOG 到 dist，方便随 exe 一起分发
    for extra in ("README.md", "CHANGELOG.md", "使用说明.txt"):
        src = ROOT / extra
        if src.exists():
            shutil.copy2(src, DIST / src.name)

    print(f"[4/4] 构建完成，用时 {time.perf_counter() - t0:.1f}s")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--debug", action="store_true", help="保留控制台窗口")
    args = ap.parse_args()
    return build(debug=args.debug)


if __name__ == "__main__":
    sys.exit(main())
