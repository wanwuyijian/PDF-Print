# -*- coding: utf-8 -*-
"""
当 github.com:443 不通、只有 api.github.com 可用时，把本地 HEAD 强制对齐到远端。

与 push_via_api.py 的区别：那个只做快进推送（一次一个提交、parents=[远端]）。
这里处理的是**改写已推送的提交**（amend / rebase 之后），需要：
    1) 以本地提交的父提交（远端已有）为 base tree
    2) 上传改动文件的 blob
    3) 用本地提交的原始 metadata 重建 commit —— SHA 必须与本地一致
    4) 强制更新 refs/heads/main 与 refs/tags/<tag>

用法：
    set GH_TOKEN=...        （或先跑 gh auth token 取值）
    python tools/force_push_via_api.py [--tag v1.0.5]
"""
import base64
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPO = "wanwuyijian/PDF-Print"
API = f"https://api.github.com/repos/{REPO}"

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


def git(*args) -> str:
    # core.quotepath=false 必须加：否则中文文件名会被转义成 "docs/\344\270\273..."，
    # 直接拿去打开文件会报 [Errno 22] Invalid argument。
    return subprocess.run(["git", "-c", "core.quotepath=false", *args], cwd=ROOT,
                          capture_output=True, text=True,
                          encoding="utf-8", errors="replace").stdout.strip()


def api(method: str, path: str, body=None):
    url = path if path.startswith("http") else API + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "pdfprint-force-push",
        **({"Content-Type": "application/json"} if data else {}),
    })
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:500]
        raise RuntimeError(f"{method} {path} → HTTP {e.code}: {detail}") from None


def split_ident(s: str) -> dict:
    lt, gt = s.rindex("<"), s.index(">")
    name, email = s[:lt].strip(), s[lt + 1:gt]
    rest = s[gt + 1:].strip().split(" ")
    ts = int(rest[0])
    tz = rest[1] if len(rest) > 1 else "+0000"
    sign = -1 if tz[0] == "-" else 1
    minutes = sign * (int(tz[1:3]) * 60 + int(tz[3:5]))
    dt = datetime.fromtimestamp(ts, tz=timezone(timedelta(minutes=minutes)))
    return {"name": name, "email": email, "date": dt.isoformat()}


def force_sync(tag: str | None = None) -> bool:
    head = git("rev-parse", "HEAD")
    parent = git("rev-parse", "HEAD^")
    branch = git("rev-parse", "--abbrev-ref", "HEAD")

    remote_branch = api("GET", f"/git/ref/heads/{branch}")["object"]["sha"]
    print(f"本地 HEAD : {head[:7]}  (parent {parent[:7]})")
    print(f"远端 {branch}: {remote_branch[:7]}")

    if remote_branch == head:
        print("远端已一致，无需推送")
    else:
        # 父提交必须远端可见，否则无法在远端拼出这棵树
        try:
            api("GET", f"/git/commits/{parent}")
        except RuntimeError as e:
            print(f"父提交 {parent[:7]} 不在远端：{e}")
            return False

        changed = git("diff", "--name-status", parent, head).splitlines()
        print(f"变更文件 {len(changed)} 个:")
        base_tree = api("GET", f"/git/commits/{parent}")["tree"]["sha"]

        entries = []
        for line in changed:
            parts = line.split("\t")
            status, path = parts[0], parts[-1]
            local = ROOT / path
            if status.startswith("D"):
                entries.append({"path": path, "mode": "100644",
                                "type": "blob", "sha": None})
                print(f"  - {path}")
                continue
            blob = api("POST", "/git/blobs", {
                "content": base64.b64encode(local.read_bytes()).decode("ascii"),
                "encoding": "base64",
            })["sha"]
            local_sha = subprocess.run(["git", "hash-object", str(local)], cwd=ROOT,
                                       capture_output=True, text=True,
                                       encoding="utf-8").stdout.strip()
            if blob != local_sha:
                print(f"  ! blob SHA 不一致 {path}: API={blob} local={local_sha}")
                return False
            mode = git("ls-tree", head, "--", path).split()[0] if status != "D" else "100644"
            entries.append({"path": path, "mode": mode, "type": "blob", "sha": blob})
            print(f"  ✓ {path}  {blob[:7]}")

        tree = api("POST", "/git/trees",
                   {"base_tree": base_tree, "tree": entries})["sha"]
        if tree != git("rev-parse", f"{head}^{{tree}}"):
            print(f"  ! tree 不一致 API={tree} local={git('rev-parse', head + '^{tree}')}")
            return False
        print(f"  ✓ tree {tree[:7]}")

        raw = subprocess.run(["git", "cat-file", "commit", head], cwd=ROOT,
                             capture_output=True).stdout.decode("utf-8", "replace")
        hdr = {}
        for line in raw.split("\n"):
            if not line.strip():
                break
            k, _, v = line.partition(" ")
            hdr[k] = v
        msg = raw.split("\n\n", 1)[1] if "\n\n" in raw else ""

        commit = api("POST", "/git/commits", {
            "message": msg, "tree": tree, "parents": [parent],
            "author": split_ident(hdr["author"]),
            "committer": split_ident(hdr["committer"]),
        })["sha"]
        if commit != head:
            print(f"  ! commit 不一致 API={commit} local={head}（未更新远端 ref）")
            return False
        print(f"  ✓ commit {commit[:7]}")

        api("PATCH", f"/git/refs/heads/{branch}", {"sha": commit, "force": True})
        print(f"分支已对齐：{remote_branch[:7]} → {commit[:7]}")

    if tag:
        try:
            api("PATCH", f"/git/refs/tags/{tag}", {"sha": head, "force": True})
            print(f"标签 {tag} 已指向 {head[:7]}")
        except RuntimeError as e:
            if "404" in str(e):
                api("POST", "/git/refs", {"ref": f"refs/tags/{tag}", "sha": head})
                print(f"标签 {tag} 已创建 → {head[:7]}")
            else:
                raise
    return True


if __name__ == "__main__":
    tag = None
    if "--tag" in sys.argv:
        tag = sys.argv[sys.argv.index("--tag") + 1]
    try:
        sys.exit(0 if force_sync(tag) else 1)
    except Exception as e:
        print(f"失败：{e}")
        sys.exit(1)
