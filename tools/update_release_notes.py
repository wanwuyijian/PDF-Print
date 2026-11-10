# -*- coding: utf-8 -*-
"""更新 Release 正文（UTF-8 走 API，不经过 PowerShell 命令行编码）。"""
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPO = "wanwuyijian/PDF-Print"
API = f"https://api.github.com/repos/{REPO}"
TAG = "v1.0.5"
TITLE = "v1.0.5 —— 页间效果统一 / 纸张·底灰·灰尘·条纹 / 预览窗口可调打印"

TOKEN = os.environ.get("GH_TOKEN", "").strip()
if not TOKEN:
    TOKEN = subprocess.run(["gh", "auth", "token"], capture_output=True,
                           text=True, timeout=30).stdout.strip()

body = (ROOT / ".git" / "RELEASE_NOTES_v105.md").read_text(encoding="utf-8")

HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "application/vnd.github+json",
    "Content-Type": "application/json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "pdfprint-release-notes",
}

# PATCH 只能打到 /releases/{id}，/releases/tags/{tag} 只支持 GET，
# 对那个路径发 PATCH 会得到 404（不是权限问题，是路由不存在）。
with urllib.request.urlopen(urllib.request.Request(
        f"{API}/releases/tags/{TAG}", headers=HEADERS), timeout=60) as r:
    rid = json.loads(r.read().decode("utf-8"))["id"]

req = urllib.request.Request(
    f"{API}/releases/{rid}",
    data=json.dumps({"body": body, "name": TITLE}).encode("utf-8"),
    method="PATCH",
    headers=HEADERS)
try:
    with urllib.request.urlopen(req, timeout=90) as r:
        d = json.loads(r.read().decode("utf-8"))
    print("名称:", d["name"])
    print("地址:", d["html_url"])
    print("正文长度:", len(d["body"]))
    print("附件:", ", ".join(a["name"] for a in d["assets"]))
except urllib.error.HTTPError as e:
    print("失败:", e.code, e.read().decode("utf-8", "replace")[:400])
    sys.exit(1)
