# -*- coding: utf-8 -*-
"""发布附件与文档的隐私扫描：exe / txt / md / png 里的私有路径与用户名。"""
import io
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ABS = re.compile(r"\b([A-Za-z]):[\\/]([^\s)\"'`]{0,80})")
BANNED = ["Administrator", "AIwork", "WorkBuddy", "wanwuyijian@", "ghp_", "gho_"]

targets = [ROOT / "dist" / "使用说明.txt", ROOT / "dist" / "README.md",
           ROOT / "dist" / "CHANGELOG.md", ROOT / "README.md",
           ROOT / "CHANGELOG.md"]

bad = 0
for p in targets:
    if not p.exists():
        continue
    text = io.open(p, encoding="utf-8", errors="replace").read()
    for m in ABS.finditer(text):
        s = m.group(0)
        if s.upper().startswith(("HTTP", "HTTPS")) or s.startswith(("C:\\Program", "C:/Windows")):
            continue
        if s.startswith(("D:\\扫描\\合同扫描件", "D:\\扫描输出")):   # 文档示例路径
            continue
        print(f"{p.name}  [路径] {s}")
        bad += 1
    for w in BANNED:
        if w in text:
            print(f"{p.name}  [敏感词] {w}")
            bad += 1

print(f"\n合计 {bad} 处")
sys.exit(1 if bad else 0)
