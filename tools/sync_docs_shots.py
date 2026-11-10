# -*- coding: utf-8 -*-
"""把 test/ui 下新截的界面图复制到 docs（截图与文档永远保持同一张）。"""
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
PAIRS = {
    "ui_tab1_print.png": "主界面.png",
    "ui_tab3_layout.png": "版式与输出.png",
    "ui_preview_print.png": "预览窗口.png",
}

for src, dst in PAIRS.items():
    a = ROOT / "test" / "ui" / src
    b = ROOT / "docs" / dst
    Image.open(a).save(b, optimize=True)
    print(f"{src} -> docs/{dst}  {Image.open(b).size}")
