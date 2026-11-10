# -*- coding: utf-8 -*-
"""发布前隐私自检：扫描所有已跟踪的文本文件，找出本机私有路径、用户名、令牌。"""
import io
import re
import subprocess
import sys

ROOT = r"H:\AIwork\SoftwareProject\PdfPrint"

files = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True,
                       text=True, encoding="utf-8", errors="replace").stdout.split()

TEXT_EXT = (".py", ".md", ".txt", ".json", ".yml", ".yaml", ".bat", ".ps1",
            ".spec", ".cfg", ".ini", ".toml")

# 绝对路径：字母盘符 + :\ 或 :/ ，后面跟路径字符
ABS_PATH = re.compile(r"\b([A-Za-z]):[\\/]([^\s)\"'`]{0,80})")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
TOKEN = re.compile(r"(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
                   r"|sk-[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16})")

# 允许出现的“假路径”：文档示例、占位符、系统自带字体
ALLOW_PREFIX = (
    "D:\\扫描输出", "D:/扫描输出",
    "D:\\扫描\\合同扫描件",     # 文档里的示例输出目录
    "D:\\某个\\路径",           # 测试里的示例粘贴文本
    "C:\\Program",              # C:\Program Files，安装说明
    "C:/Windows/Fonts",         # 系统字体，脚本要用
    "C:\\path", "X:\\",
)

hits = 0
for f in files:
    if not f.lower().endswith(TEXT_EXT):
        continue
    try:
        text = io.open(f, encoding="utf-8", errors="replace").read()
    except Exception:
        continue
    for lineno, line in enumerate(text.splitlines(), 1):
        for m in ABS_PATH.finditer(line):
            s = m.group(0)
            if s.upper().startswith(("HTTP", "HTTPS")):
                continue
            if s.startswith(ALLOW_PREFIX):
                continue
            print(f"{f}:{lineno}  [绝对路径] {s[:90]}")
            hits += 1
        for m in EMAIL.finditer(line):
            s = m.group(0)
            if s.endswith(("example.com", "users.noreply.github.com")):
                continue
            print(f"{f}:{lineno}  [邮箱] {s}")
            hits += 1
        for m in TOKEN.finditer(line):
            print(f"{f}:{lineno}  [疑似令牌] {m.group(0)[:24]}...")
            hits += 1

print(f"\n合计 {hits} 处")
sys.exit(1 if hits else 0)
