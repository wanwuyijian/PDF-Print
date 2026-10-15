# -*- coding: utf-8 -*-
"""
在 github.com 不通、只有 api.github.com 可用时，通过 Git Data API 推送提交。

为什么能保持本地/远端不分叉：
    git 的 blob / tree / commit 都是**内容寻址**的 —— SHA 由内容 + 元数据算出。
    GitHub API 用的是同一套 git 对象格式，所以只要内容与元数据一致，
    通过 API 造出来的对象 SHA 与本地 `git hash-object` 完全相同。
    脚本最后会逐个校验 SHA 是否吻合，不吻合就中止，不留下脏状态。

用法：
    python tools/push_via_api.py
"""
import base64
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPO = "wanwuyijian/PDF-Print"
API = f"https://api.github.com/repos/{REPO}"

TOKEN = os.environ.get("GH_TOKEN", "").strip()
if not TOKEN:
    print("需要 GH_TOKEN 环境变量"); sys.exit(1)


def git(*args) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True,
                          text=True, encoding="utf-8", errors="replace").stdout.strip()


def api(method: str, path: str, body=None):
    import urllib.request
    url = path if path.startswith("http") else API + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "pdfprint-push-script",
        **({"Content-Type": "application/json"} if data else {}),
    })
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:400]
        raise RuntimeError(f"{method} {path} → HTTP {e.code}: {detail}") from None


def branch() -> str:
    return git("rev-parse", "--abbrev-ref", "HEAD")


def push_one_commit():
    """把本地领先远端的提交通过 API 推上去"""
    head = git("rev-parse", "HEAD")
    remote = api("GET", f"/git/ref/heads/{branch()}")["object"]["sha"]
    ahead = git("rev-list", f"{remote}..{head}").split()
    if not ahead:
        print(f"本地与远端已同步（{head[:7]}），无需推送")
        return True
    print(f"远端 HEAD : {remote[:7]}")
    print(f"本地 HEAD : {head[:7]}")
    print(f"待推提交 : {len(ahead)} 个")
    if len(ahead) > 1:
        print("！一次只处理 1 个提交，请先 squash")
        return False

    # 本次提交相对远端改了哪些文件
    changed = git("diff", "--name-status", remote, head).splitlines()
    print("变更文件:")
    for line in changed:
        print("   ", line)

    base_tree = api("GET", f"/git/commits/{remote}")["tree"]["sha"]

    tree_entries = []
    for line in changed:
        parts = line.split("\t")
        status, path = parts[0], parts[-1]
        local = ROOT / path
        if status.startswith("D"):
            tree_entries.append({"path": path, "mode": "100644",
                                 "type": "blob", "sha": None})
            continue
        blob = api("POST", "/git/blobs", {
            "content": base64.b64encode(local.read_bytes()).decode("ascii"),
            "encoding": "base64",
        })["sha"]
        # 内容寻址：API 算出的 blob SHA 必须等于本地 git hash-object
        local_sha = subprocess.run(
            ["git", "hash-object", str(local)], cwd=ROOT,
            capture_output=True, text=True, encoding="utf-8").stdout.strip()
        if blob != local_sha:
            print(f"  ! blob SHA 不一致 {path}: API={blob} local={local_sha}")
            return False
        print(f"  ✓ blob {path}  {blob[:7]} (与本地一致)")
        tree_entries.append({"path": path, "mode": "100644", "type": "blob", "sha": blob})

    new_tree = api("POST", "/git/trees",
                   {"base_tree": base_tree, "tree": tree_entries})["sha"]
    local_tree = git("rev-parse", f"{head}^{{tree}}")
    if new_tree != local_tree:
        print(f"  ! tree SHA 不一致 API={new_tree} local={local_tree}")
        return False
    print(f"  ✓ tree {new_tree[:7]} (与本地一致)")

    raw = subprocess.run(["git", "cat-file", "commit", head], cwd=ROOT,
                         capture_output=True).stdout.decode("utf-8", "replace")
    hdr = {}
    for line in raw.split("\n"):
        if not line.strip():
            break
        k, _, v = line.partition(" ")
        hdr[k] = v
    msg = raw.split("\n\n", 1)[1] if "\n\n" in raw else ""

    def split_ident(s):
        # git 身份行: "Name <email> <unix_ts> +0800"
        # GitHub API 要的是 ISO 8601（"YYYY-MM-DDTHH:MM:SS+08:00"），
        # 但只要能无损换算回同一个 (unix_ts, 偏移量)，生成的 commit SHA 就一致。
        lt, gt = s.rindex("<"), s.index(">")
        name, email = s[:lt].strip(), s[lt + 1:gt]
        rest = s[gt + 1:].strip().split(" ")
        ts = int(rest[0])
        tz = rest[1] if len(rest) > 1 else "+0000"
        sign = -1 if tz[0] == "-" else 1
        minutes = sign * (int(tz[1:3]) * 60 + int(tz[3:5]))
        dt = datetime.fromtimestamp(ts, tz=timezone(timedelta(minutes=minutes)))
        return {"name": name, "email": email, "date": dt.isoformat()}

    commit = api("POST", "/git/commits", {
        "message": msg, "tree": new_tree, "parents": [remote],
        "author": split_ident(hdr["author"]),
        "committer": split_ident(hdr["committer"]),
    })["sha"]
    if commit != head:
        print(f"  ! commit SHA 不一致 API={commit} local={head}")
        print("    （多半是日期格式问题，未更新远端 ref，安全中止）")
        return False
    print(f"  ✓ commit {commit[:7]} (与本地一致)")

    api("PATCH", f"/git/refs/heads/{branch()}", {"sha": commit, "force": False})
    print(f"\n推送完成：{remote[:7]} → {commit[:7]}")
    return True


if __name__ == "__main__":
    try:
        ok = push_one_commit()
    except Exception as e:
        print(f"失败：{e}")
        ok = False
    sys.exit(0 if ok else 1)