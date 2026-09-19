#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用 Git Data API 推送本地提交（github.com 的 smart-HTTP 走不通时的替代路径）。

为什么需要它
------------
境内网络下 github.com 的部分 A 记录会 TCP 超时，典型症状是 `git push`
**静默失败或长时间挂起**（既不报错也不推进），而 `gh` / `api.github.com`
一切正常 —— 因为后者走的是另一组域名。于是绕开 git 的 smart-HTTP，
直接打 REST API：

    1. GET   /git/ref/heads/<branch>   取远端当前提交，作为新提交的父节点
    2. POST  /git/trees                内联 content，一次调用建整棵树
                                       （不要每文件一次 blob API —— 长流程容易被掐断）
    3. POST  /git/commits              带父节点，历史连续
    4. POST  /git/refs                 创建；已存在则 PATCH 更新

它只搬运**已被 git 提交的内容**（读的是 index / HEAD，不是工作区），
所以不要指望它顺手帮你 `git add`。

验证方式
--------
主判据是**远端根 tree 的 sha 与本地 HEAD^{tree} 相等**，而不是逐文件比对。
tree sha 是 (名字, 模式, 内容) 的完整函数，相等即整棵树逐字节相同。

为什么逐文件比对不够：路径里的非 ASCII 字符会被 `git ls-files -s` 按
`core.quotePath` 规则 quoting（`"docs/\346\225..."`）。把这个带引号的转义串
当路径交给 trees API，GitHub 会忠实地建出一个名叫 `"docs` 的目录、里面放一个
名叫 `\346\225...md` 的文件 —— 内容全对、**目录结构全错**。更阴的是逐文件
比对会全绿（两侧拿到的是同一个错误字符串），只有根 tree sha 能立刻指出
"多了一个 `"docs` 目录、少了 `docs`"。

用法
----
    python build/push_api.py <本地仓库目录> <owner/repo>
    python build/push_api.py . DSMFRP1024/hermes-offline-arm64 --branch main

代理从环境变量读（https_proxy / HTTPS_PROXY / http_proxy / HTTP_PROXY），
令牌从环境变量 GH_TOKEN/GITHUB_TOKEN 或 `gh auth token` 取。
"""

from __future__ import annotations

import base64
import http.client
import json
import os
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

API = "https://api.github.com"
UA = "hermes-offline-push/2.0"

# 实测：某些环境（带进程审计/沙箱）里每次 spawn git 要约 5s。
# 所以凡"每个文件一次 git 调用"的写法都会把整体拖到分钟级，
# 必须批量化；同时给每次调用上超时，避免无声挂死。
GIT_TIMEOUT = 60
HTTP_TIMEOUT = 120
# 传输层失败（TLS 握手超时 / 连接重置 / DNS 抖动）的重试次数。
# 见 req()：这几个接口是幂等的，重试安全。
HTTP_RETRIES = 4


# ---------------------------------------------------------------------------
# 日志：边跑边落盘
# ---------------------------------------------------------------------------

class Log:
    """逐行 flush 的日志。

    刻意**不**在 `finally` 里一次性写盘：进程被外部超时杀掉时 `finally`
    根本不会执行，事后一点诊断信息都没有 —— 这个坑真踩过。
    """

    def __init__(self, path: Path | None):
        self.fh = (path.open("w", encoding="utf-8", newline="\n")
                   if path else None)

    def __call__(self, msg: object = "") -> None:
        print(msg, flush=True)
        if self.fh:
            self.fh.write(str(msg) + "\n")
            self.fh.flush()

    def close(self) -> None:
        if self.fh:
            self.fh.close()
            self.fh = None


# ---------------------------------------------------------------------------
# git / HTTP 基础
# ---------------------------------------------------------------------------

def git(repo: str, *args: str, binary: bool = False, check: bool = True):
    p = subprocess.run(["git", "-C", repo, *args],
                       capture_output=True, timeout=GIT_TIMEOUT)
    if check and p.returncode != 0:
        raise SystemExit(
            f"✗ git {' '.join(args)} 失败（rc={p.returncode}）："
            f"{p.stderr.decode('utf-8', 'replace').strip()[:400]}")
    return p.stdout if binary else p.stdout.decode("utf-8", "replace")


def pick_proxy() -> str | None:
    """按明确优先级取代理。

    不直接用 urllib 的 ProxyHandler()（无参=读环境）是因为大小写两套变量
    谁覆盖谁在不同 Python 版本上不一致，这里自己定死顺序，行为可预期。
    """
    for k in ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY"):
        v = os.environ.get(k)
        if v:
            return v.strip()
    return None


def opener(proxy: str | None):
    if proxy:
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    return urllib.request.build_opener(urllib.request.ProxyHandler())


def token() -> str:
    t = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if t:
        return t.strip()
    p = subprocess.run(["gh", "auth", "token"], capture_output=True,
                       text=True, timeout=30)
    t = (p.stdout or "").strip()
    if not t:
        raise SystemExit(f"✗ 拿不到令牌：`gh auth token` 返回空（{p.stderr.strip()[:200]}）")
    return t


def req(method: str, url: str, tok: str, op, body=None, log=None):
    """发一次 GitHub API 请求。

    **传输层失败要重试**（HTTPError 不重试，那是"请求本身有问题"，要原样交给
    调用方判断）。理由：跨境链路上 TLS 握手超时 / 连接被重置是常态，一次抖动
    就让整轮推送白跑 —— 实测 `GET /git/ref/...` 撞上一次
    `TimeoutError: _ssl.c:1015: The handshake operation timed out`，
    前面准备好的整棵 tree 全废。

    这里重试是安全的：用一个固定 tok，失败发生在**建连阶段**时请求根本没到服务端；
    即便偶发"服务端处理完了但响应没回来"，Git Data API 这几个接口也是幂等的 ——
    `POST /git/trees`、`POST /git/commits` 同样的内容必然得到同样的 sha
    （重复创建只会留下一个内容相同的悬空对象），`PATCH /git/refs` 用同一个 sha
    再打一次结果不变。最后还有"远端根 tree sha == 本地 HEAD^{tree}"的回读校验兜底。
    """
    data = json.dumps(body).encode("utf-8") if body is not None else None
    last: Exception | None = None
    for attempt in range(1, HTTP_RETRIES + 1):
        r = urllib.request.Request(url, data=data, method=method)
        r.add_header("Authorization", f"Bearer {tok}")
        r.add_header("Accept", "application/vnd.github+json")
        r.add_header("X-GitHub-Api-Version", "2022-11-28")
        r.add_header("User-Agent", UA)
        if data:
            r.add_header("Content-Type", "application/json")
        try:
            with op.open(r, timeout=HTTP_TIMEOUT) as resp:
                raw = resp.read().decode("utf-8")
                return resp.status, (json.loads(raw) if raw.strip() else {})
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            try:
                return e.code, json.loads(raw)
            except ValueError:
                return e.code, {"raw": raw[:800]}
        except (urllib.error.URLError, ssl.SSLError, OSError,
                http.client.HTTPException) as e:
            last = e
            if attempt < HTTP_RETRIES:
                if log:
                    log(f"  · {method} {url.split('?')[0][-60:]} 传输失败"
                        f"（{type(e).__name__}: {e}），重试 {attempt - 1}"
                        f"/{HTTP_RETRIES - 1}")
                time.sleep(min(2.0 * attempt, 8.0))
    raise RuntimeError(f"{method} {url} 连续 {HTTP_RETRIES} 次传输失败：{last}")


# ---------------------------------------------------------------------------
# 本地状态
# ---------------------------------------------------------------------------

def local_files(repo: str) -> list[tuple[str, str, bytes]]:
    """→ [(路径, 模式, blob 字节)]，一次批量取完。

    必须用 `-z`（NUL 分隔）。默认的 `git ls-files -s` 会对含非 ASCII 的路径做
    quoting（core.quotePath），输出的**带引号转义串**会被当成真实路径 ——
    详见模块开头"验证方式"一节。
    """
    raw = git(repo, "ls-files", "-s", "-z", binary=True)
    entries: list[tuple[str, str, str]] = []
    for rec in raw.split(b"\x00"):
        if not rec:
            continue
        meta, raw_path = rec.split(b"\t", 1)
        mode, sha, _stage = meta.decode("ascii").split()
        entries.append((raw_path.decode("utf-8"), mode, sha))

    if not entries:
        return []

    # 一次 `cat-file --batch` 取代 N 次 `cat-file blob`：
    # 在这台机器上每个进程约 5s，14 个文件就是 70s 的纯开销。
    stdin = "".join(f"{sha}\n" for _, _, sha in entries).encode("ascii")
    p = subprocess.run(["git", "-C", repo, "cat-file", "--batch"],
                       input=stdin, capture_output=True, timeout=GIT_TIMEOUT)
    if p.returncode != 0:
        raise SystemExit(f"✗ git cat-file --batch 失败："
                         f"{p.stderr.decode('utf-8', 'replace')[:400]}")

    out, pos, blobs = p.stdout, 0, []
    for (path, _mode, sha) in entries:
        nl = out.find(b"\n", pos)
        if nl < 0:
            raise SystemExit(f"✗ cat-file --batch 输出不完整（取 {path} 时断流）")
        header = out[pos:nl].decode("utf-8", "replace").split()
        if len(header) < 3:
            raise SystemExit(f"✗ cat-file 对 {sha[:12]} 返回异常头：{header}")
        size = int(header[2])
        blobs.append(out[nl + 1:nl + 1 + size])
        pos = nl + 1 + size + 1        # 跳过分隔内容的换行
    return [(path, mode, blob)
            for (path, mode, _), blob in zip(entries, blobs)]


def head_info(repo: str) -> tuple[str, str, str]:
    """一次调用取回 HEAD 的提交 sha、tree sha 和提交信息。"""
    fmt = "%H%n%T%n%B"
    s = git(repo, "log", "-1", f"--pretty=format:{fmt}")
    lines = s.split("\n")
    return lines[0].strip(), lines[1].strip(), "\n".join(lines[2:]).rstrip("\n")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    opts = [a for a in sys.argv[1:] if a.startswith("--")]
    if len(args) < 2:
        print(__doc__)
        return 2
    repo, slug = args[0], args[1]

    branch = "main"
    for i, o in enumerate(opts):
        if o == "--branch" and i + 1 < len(opts):
            branch = opts[i + 1]

    log_path = Path(repo) / ".push-api.log"
    log = Log(log_path)
    try:
        return run(repo, slug, branch, log)
    finally:
        log.close()


def run(repo: str, slug: str, branch: str, log: Log) -> int:
    proxy = pick_proxy()
    tok = token()
    ref = f"refs/heads/{branch}"
    op = opener(proxy)

    log(f"仓库    : {repo}")
    log(f"目标    : {slug}")
    log(f"分支    : {branch}")
    log(f"代理    : {proxy or '(直连)'}")
    log("")

    files = local_files(repo)
    head_sha, local_tree, msg = head_info(repo)
    log(f"本地跟踪文件 : {len(files)} 个")
    log(f"本地 HEAD    : {head_sha}")
    log(f"本地 root tree: {local_tree}")

    # 远端当前 ref（有则作为父节点，保留历史；没有就是根提交）
    st, res = req("GET", f"{API}/repos/{slug}/git/ref/{ref.lstrip('refs/')}",
                  tok, op)
    parent = res.get("object", {}).get("sha") if st == 200 else None
    log(f"远端 {ref} : {parent or '(不存在，将创建根提交)'}")
    log("")

    # ── 1. 建树 ──
    tree = []
    for path, mode, blob in files:
        try:
            entry = {"path": path, "mode": mode, "type": "blob",
                     "content": blob.decode("utf-8")}
        except UnicodeDecodeError:
            entry = {"path": path, "mode": mode, "type": "blob",
                     "content": base64.b64encode(blob).decode("ascii"),
                     "encoding": "base64"}
        tree.append(entry)

    log(f"→ POST /git/trees（内联 {len(tree)} 个文件）")
    # 刻意不传 base_tree：我们给的是**完整**目录树（来自 git index），
    # 让它从空树开始建，正好可以和本地 HEAD^{tree} 做逐字节相等的比对。
    # 若传了 base_tree，GitHub 会做一次增量合并，远端 tree 里就会混进
    # 本地 index 之外的条目，那条"sha 必须相等"的判据随即失效。
    st, res = req("POST", f"{API}/repos/{slug}/git/trees", tok, op, {"tree": tree})

    # 空仓库调 Git Data API 一律 409（"Git Repository is empty."）。
    # 先用 Contents API PUT 一个文件把仓库"点活"，再重试建树。
    # 要把"API 查询失败"和"真的为空"分开，否则一次网络抖动就会误判。
    if st == 409 and "empty" in json.dumps(res).lower():
        log("  仓库为空（409），用 Contents API 引导一次...")
        boot = {"message": "chore: bootstrap for Git Data API push",
                "content": base64.b64encode(b"bootstrap\n").decode("ascii")}
        st_b, res_b = req("PUT", f"{API}/repos/{slug}/contents/.bootstrap",
                          tok, op, boot)
        if st_b not in (200, 201):
            log(f"✗ bootstrap 失败 {st_b}: {res_b}")
            return 1
        parent = None
        log(f"  ✓ 已引导（{res_b['commit']['sha'][:12]}，随后不可达）")
        st, res = req("POST", f"{API}/repos/{slug}/git/trees", tok, op, body)

    if st not in (200, 201):
        log(f"✗ 建树失败 {st}: {res}")
        return 1
    remote_tree = res["sha"]
    log(f"  远端 tree = {remote_tree}")
    log(f"  与本地 root tree 一致? "
        f"{'是 ✓' if remote_tree == local_tree else '否 ✗（内容有差异）'}")
    log("")

    # ── 2. 建提交 ──
    commit_body = {"message": msg or "update", "tree": remote_tree}
    if parent:
        commit_body["parents"] = [parent]
    log(f"→ POST /git/commits（{'父节点 ' + parent[:12] if parent else '根提交'}）")
    st2, res2 = req("POST", f"{API}/repos/{slug}/git/commits", tok, op, commit_body)
    if st2 not in (200, 201):
        log(f"✗ 建提交失败 {st2}: {res2}")
        return 1
    commit = res2["sha"]
    log(f"  远端 commit = {commit}")
    log("")

    # ── 3. 更新 ref ──
    if parent:
        log("→ PATCH /git/refs（已存在，更新）")
        st3, res3 = req("PATCH", f"{API}/repos/{slug}/git/refs/heads/{branch}",
                        tok, op, {"sha": commit, "force": True})
        if st3 not in (200, 201):
            log(f"✗ 更新 ref 失败 {st3}: {res3}")
            return 1
        log(f"  ✓ {ref} 已更新 {parent[:12]} → {commit[:12]}")
    else:
        log("→ POST /git/refs（创建）")
        st3, res3 = req("POST", f"{API}/repos/{slug}/git/refs", tok, op,
                        {"ref": ref, "sha": commit})
        if st3 in (200, 201):
            log(f"  ✓ {ref} 已创建")
        else:
            log(f"  创建返回 {st3}，改用 PATCH 强制更新")
            st4, res4 = req("PATCH", f"{API}/repos/{slug}/git/refs/heads/{branch}",
                            tok, op, {"sha": commit, "force": True})
            if st4 not in (200, 201):
                log(f"✗ 更新 ref 失败 {st4}: {res4}")
                return 1
            log(f"  ✓ {ref} 已更新")
    log("")

    # ── 4. 回读校验 ──
    log("→ 回读校验")
    if remote_tree == local_tree:
        log(f"  ✓ 根 tree sha 相等（{remote_tree}）—— 整棵目录树与本地逐字节相同")
    else:
        log(f"  ✗ 根 tree sha 不同：远端 {remote_tree} / 本地 {local_tree}")

    st5, res5 = req("GET", f"{API}/repos/{slug}/git/trees/{remote_tree}?recursive=1",
                    tok, op)
    if st5 == 200:
        remote = {e["path"]: (e["mode"], e["type"], e["sha"])
                  for e in res5.get("tree", [])}
        raw = git(repo, "ls-tree", "-r", "-t", "-z", "HEAD", binary=True)
        lo = {}
        for rec in raw.split(b"\x00"):
            if not rec:
                continue
            meta, p = rec.split(b"\t", 1)
            m, t, s = meta.decode("ascii").split()
            lo[p.decode("utf-8")] = (m, t, s)
        log(f"  条目数：远端 {len(remote)} / 本地 {len(lo)}")
        if remote == lo:
            log("  ✓ 递归条目（含 tree）全部一致")
        else:
            log("  ✗ 有差异：")
            for k in sorted(set(remote) | set(lo)):
                if remote.get(k) != lo.get(k):
                    log(f"      {k}")
                    log(f"          远端={remote.get(k)}")
                    log(f"          本地={lo.get(k)}")
    else:
        log(f"  回读失败 {st5}: {res5}")

    log("")
    ok = remote_tree == local_tree
    log(f"{'✅' if ok else '❌'} https://github.com/{slug}/tree/{branch}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
