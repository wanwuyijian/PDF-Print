# -*- coding: utf-8 -*-
"""
修复 GitHub Release 附件名（中文被 PowerShell 参数编码破坏成 PDF._v1.0.5.exe /
default.txt），并确保 tag 指向脱敏后的提交。

做法：删掉坏附件，用 API 以 UTF-8 文件名重新上传。
Release 附件名是通过 multipart 的 filename 字段传的，API 全程走 JSON/HTTP，
不经过 PowerShell 的命令行编码，所以中文能原样保留。

用法：
    set GH_TOKEN=...
    python tools/fix_release_assets.py
"""
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

TOKEN = os.environ.get("GH_TOKEN", "").strip()
if not TOKEN:
    try:
        TOKEN = subprocess.run(["gh", "auth", "token"], capture_output=True,
                               text=True, timeout=30).stdout.strip()
    except Exception:
        TOKEN = ""
if not TOKEN:
    print("缺少令牌：设置 GH_TOKEN，或确认 gh 已登录")
    sys.exit(1)

# 期望的附件（本地文件 → 上传后的显示名）
#
# 注意：GitHub 存储附件名时会把非 ASCII 字符**直接删掉**，而不是转义 ——
# 「PDF 打印扫描模拟器_v1.0.5.exe」会被存成「PDF._v1.0.5.exe」，
# 「使用说明.txt」会被存成「default.txt」。这不是客户端编码问题，
# 用 percent-encoding 传 filename*/name 参数也绕不过去。
# 所以附件名一律用 ASCII，中文说明放在 release 正文里。
ASSETS = [
    (ROOT / "dist" / "PDF 打印扫描模拟器_v1.0.5.exe", "PdfPrintScanSimulator_v1.0.5.exe"),
    (ROOT / "dist" / "使用说明.txt", "README_v1.0.5.txt"),
]


def api(method: str, path: str, body=None, raw: bytes | None = None,
        ctype: str | None = None, accept: str = "application/vnd.github+json"):
    url = path if path.startswith("http") else API + path
    data = raw if raw is not None else (
        json.dumps(body).encode("utf-8") if body is not None else None)
    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "pdfprint-release-fix",
    }
    if accept:
        headers["Accept"] = accept
    if ctype:
        headers["Content-Type"] = ctype
    elif data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            payload = r.read().decode("utf-8") or "{}"
            return json.loads(payload)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:500]
        raise RuntimeError(f"{method} {path} → HTTP {e.code}: {detail}") from None


def release() -> dict:
    return api("GET", f"/releases/tags/{TAG}")


def main() -> int:
    rel = release()
    rid = rel["id"]
    print(f"Release {TAG}: {rel['html_url']}")
    print(f"tag 指向: {rel['target_commitish']}")

    existing = rel.get("assets", [])
    print(f"现有附件 {len(existing)} 个:")
    for a in existing:
        print(f"   {a['name']}  ({a['size'] / 1048576:.2f} MB)")

    # 1. 删掉名字坏掉的附件
    for a in existing:
        name = a["name"]
        if name.endswith(".exe") or name == "default.txt" or "使用说明" not in name:
            print(f"  删除 {name}")
            api("DELETE", f"/releases/assets/{a['id']}")
        else:
            print(f"  保留 {name}")

    # 2. 重新上传，文件名以 UTF-8 原样传给 Content-Disposition
    for local, display in ASSETS:
        if not local.exists():
            print(f"！本地文件缺失：{local}")
            return 1
        data = local.read_bytes()
        boundary = "----pdfprint" + os.urandom(8).hex()
        # 附件名通过 filename* 以 RFC 5987 编码，保证中文不被转义丢弃
        safe_name = urllib.parse.quote(display, safe="")
        head = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; '
            f"filename*=UTF-8''{safe_name}\r\n"
            f"Content-Type: application/octet-stream\r\n\r\n"
        ).encode("utf-8")
        tail = f"\r\n--{boundary}--\r\n".encode("utf-8")
        body = head + data + tail
        print(f"上传 {display}  ({len(data) / 1048576:.2f} MB)")
        out = api("POST",
                  f"https://uploads.github.com/repos/{REPO}/releases/{rid}/assets"
                  f"?name={urllib.parse.quote(display)}",
                  raw=body,
                  ctype=f"multipart/form-data; boundary={boundary}")
        print(f"  → {out.get('name')}  {out.get('browser_download_url')}")

    print("\n最终附件:")
    for a in release().get("assets", []):
        print(f"   {a['name']}  ({a['size'] / 1048576:.2f} MB)")
    return 0


if __name__ == "__main__":
    import urllib.parse  # noqa: E402  （放这里，避免覆盖模块级 import）
    sys.exit(main())
