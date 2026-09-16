#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GitHub Actions 运行查询 / 产物下载 / 离线包校验。

为什么不用 `gh run download`：
    `gh` 内部用的是 Go 默认 http.Client，未设置任何超时。走代理拉几百 MB
    的 artifact 时，只要 TCP 连接进入"半死"状态（国内代理很常见），
    它就会无限挂起，连 Ctrl-C 都要等很久才响应。这里自己实现：

     - 每次读操作都有 socket 超时（默认 60s），卡住就报错而不是干等；
     - **多分片并发**：跨境链路单连接常被限速在几百 KiB/s，692 MiB 的包
       单连接要跑近一小时；切成 4 片并发能把总吞吐拉到接近 4 倍；
     - 断点续传（`<file>.partN` + `Range:` 头），断开后重跑不从头开始；
     - 下载完拿 API 返回的 `digest`（sha256）做完整性校验；
     - 从 artifact zip 里取出内层 tar.gz，再算一遍 sha256 作为最终凭据。

用法：
    python gh_run.py runs     --repo OWNER/REPO
    python gh_run.py watch    --repo OWNER/REPO --run RUN_ID
    python gh_run.py arts     --repo OWNER/REPO --run RUN_ID
    python gh_run.py download --repo OWNER/REPO --artifact ART_ID --out DIR [--parts 8]
    python gh_run.py verify   --tarball dist/hermes-offline-arm64.tar.gz

代理：自动读 HTTP_PROXY / HTTPS_PROXY 环境变量。
令牌：自动执行 `gh auth token` 获取，无需手工填。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

API = "https://api.github.com"
UA = "hermes-offline-tool/1.0"


# ───────────────────────────── 基础 HTTP ─────────────────────────────

def gh_token() -> str:
    """取 gh CLI 里已登录的令牌，避免在命令行/文件里明文写 token。"""
    tok = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if tok:
        return tok.strip()
    try:
        out = subprocess.run(["gh", "auth", "token"], capture_output=True,
                             text=True, timeout=20, check=True)
    except (subprocess.SubprocessError, FileNotFoundError) as exc:
        raise SystemExit(f"✗ 拿不到 GitHub 令牌（gh auth token 失败）：{exc}")
    tok = out.stdout.strip()
    if not tok:
        raise SystemExit("✗ `gh auth token` 返回空，请先 `gh auth login`")
    return tok


class _NoAuthOnHostChange(urllib.request.HTTPRedirectHandler):
    """artifact 的 zip 接口会 302 到 objects.githubusercontent.com 的签名 URL。

    签名 URL 本身已经带鉴权参数；如果再带着 api.github.com 的
    Authorization 头去请求，GitHub 会直接 400。所以在跨主机跳转时把头摘掉。
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is None:
            return None
        old_host = urllib.parse.urlsplit(req.full_url).hostname
        new_host = urllib.parse.urlsplit(newurl).hostname
        if old_host != new_host:
            new.headers.pop("Authorization", None)
            new.headers.pop("authorization", None)
        return new


def _opener() -> urllib.request.OpenerDirector:
    # ProxyHandler() 不传参会自动读环境变量 HTTP_PROXY / HTTPS_PROXY
    return urllib.request.build_opener(
        urllib.request.ProxyHandler(), _NoAuthOnHostChange()
    )


def api_get(path_or_url: str, token: str, timeout: float = 60.0) -> dict:
    url = path_or_url if path_or_url.startswith("http") else API + path_or_url
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": UA,
    })
    with _opener().open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def human(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024 or unit == "TiB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TiB"


# ───────────────────────────── runs / watch / arts ─────────────────────────────

def cmd_runs(args) -> int:
    token = gh_token()
    data = api_get(f"/repos/{args.repo}/actions/runs?per_page={args.limit}", token)
    runs = data.get("workflow_runs", [])
    if not runs:
        print("（没有任何运行记录）")
        return 0
    print(f"{'RUN ID':<14} {'STATUS':<12} {'CONCLUSION':<12} {'EVENT':<16} CREATED")
    for r in runs:
        print(f"{r['id']:<14} {r['status']:<12} {str(r.get('conclusion')):<12} "
              f"{r['event']:<16} {r['created_at']}")
    return 0


def _run_view(repo: str, run_id: int, token: str) -> dict:
    return api_get(f"/repos/{repo}/actions/runs/{run_id}", token)


def _jobs(repo: str, run_id: int, token: str) -> list:
    return api_get(f"/repos/{repo}/actions/runs/{run_id}/jobs?per_page=100",
                   token).get("jobs", [])


def cmd_watch(args) -> int:
    token = gh_token()
    t0 = time.time()
    last = None
    while True:
        if time.time() - t0 > args.timeout:
            print(f"\n✗ 等待超过 {args.timeout}s，放弃（构建可能仍在跑）")
            return 2
        run = _run_view(args.repo, args.run, token)
        line = f"[{run['status']:<12}] {run.get('conclusion') or '-'}"
        if run["status"] == "completed":
            print("")  # 结束前的换行
            for j in _jobs(args.repo, args.run, token):
                print(f"  job {j['name']}: {j['status']} / {j.get('conclusion')}")
                for s in j.get("steps", []):
                    mark = {"success": "✓", "failure": "✗",
                            "skipped": "-", "cancelled": "!"}.get(
                        s.get("conclusion") or "", "·")
                    print(f"      {mark} {s['name']} ({s.get('conclusion') or s['status']})")
            print(f"\n结论：{run.get('conclusion')}   "
                  f"耗时 {_dur(run.get('run_started_at'), run.get('updated_at'))}")
            print(run["html_url"])
            return 0 if run.get("conclusion") == "success" else 1
        if line != last:
            print(line)
            last = line
        time.sleep(args.interval)


def _dur(a: str | None, b: str | None) -> str:
    if not a or not b:
        return "?"
    import datetime as dt
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    try:
        s = dt.datetime.strptime(a[:19], fmt)
        e = dt.datetime.strptime(b[:19], fmt)
    except ValueError:
        return "?"
    secs = int((e - s).total_seconds())
    return f"{secs // 60}m{secs % 60:02d}s"


def cmd_arts(args) -> int:
    token = gh_token()
    data = api_get(f"/repos/{args.repo}/actions/runs/{args.run}/artifacts",
                   token)
    arts = data.get("artifacts", [])
    if not arts:
        print("（该次运行没有 artifact）")
        return 0
    for a in arts:
        print(f"id={a['id']}  name={a['name']}  size={human(a['size_in_bytes'])}  "
              f"expired={a['expired']}")
        print(f"    digest={a.get('digest')}")
        print(f"    url={a['archive_download_url']}")
    return 0


# ──────────────────── 下载（多分片并发 + 超时 + 断点续传） ────────────────────
#
# 为什么必须并发分片：
#   跨境链路上单条 TCP 连接的吞吐常被压在几百 KiB/s，而这个 artifact
#   有 692 MiB —— 单连接实测只有 ~200–350 KiB/s，要跑 50 分钟左右。
#   切成 N 个区间并发拉，总吞吐能接近 N 倍（瓶颈从"单连接"变成"总带宽"）。


class _Progress:
    """多线程共享的字节计数。"""

    def __init__(self, initial: int = 0):
        self._lock = threading.Lock()
        self._done = initial

    def add(self, n: int) -> None:
        with self._lock:
            self._done += n

    def get(self) -> int:
        with self._lock:
            return self._done


def probe_total(url: str, token: str, timeout: float) -> tuple[int | None, bool]:
    """探测 (总字节数, 是否支持 Range)。只取 1 字节，不拉整个文件。"""
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": UA,
        "Range": "bytes=0-0",
    })
    with _opener().open(req, timeout=timeout) as r:
        cr = r.headers.get("Content-Range")
        r.read(1)
        if r.status == 206 and cr and "/" in cr:
            return int(cr.rsplit("/", 1)[-1]), True
        cl = r.headers.get("Content-Length")
        return (int(cl) if cl else None), False


def _fetch_range(url: str, token: str, path: Path, start: int, end: int,
                 timeout: float, retries: int, prog: _Progress, label: int) -> None:
    """把 [start, end] 区间下到 path，区间内可断点续传。"""
    want = end - start + 1
    attempt = 0
    while True:
        have = path.stat().st_size if path.exists() else 0
        if have >= want:
            return
        attempt += 1
        try:
            req = urllib.request.Request(url, headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "User-Agent": UA,
                "Range": f"bytes={start + have}-{end}",
            })
            with _opener().open(req, timeout=timeout) as r:
                if r.status == 200 and have:
                    raise RuntimeError("服务端忽略了 Range，无法续传")
                with path.open("ab" if have else "wb") as f:
                    got = have
                    while got < want:
                        chunk = r.read(1 << 18)      # 256 KiB
                        if not chunk:
                            break
                        f.write(chunk)
                        got += len(chunk)
                        prog.add(len(chunk))
            if path.stat().st_size >= want:
                return
            raise RuntimeError(f"分片 {label} 提前结束"
                               f"（{path.stat().st_size}/{want}）")
        except Exception as exc:  # noqa: BLE001 —— 网络层什么都可能抛
            if attempt > retries:
                raise
            time.sleep(min(2 ** attempt, 30))


def _fetch_single(url: str, token: str, path: Path, timeout: float,
                  retries: int, prog: _Progress, total: int | None) -> None:
    """单连接顺序下载（服务端不支持 Range 或文件很小时的路径）。"""
    attempt = 0
    while True:
        have = path.stat().st_size if path.exists() else 0
        if total is not None and have >= total:
            return
        attempt += 1
        try:
            headers = {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "User-Agent": UA,
            }
            if have:
                headers["Range"] = f"bytes={have}-"
            req = urllib.request.Request(url, headers=headers)
            with _opener().open(req, timeout=timeout) as r:
                if r.status == 200 and have:
                    # 服务器忽略了 Range：只能从头来
                    print("\n  · 服务端不支持续传，重新开始")
                    have = 0
                    path.unlink(missing_ok=True)
                elif r.status == 416:
                    return
                cr = r.headers.get("Content-Range")
                if cr and "/" in cr:
                    total = int(cr.rsplit("/", 1)[-1])
                elif total is None:
                    cl = r.headers.get("Content-Length")
                    total = (int(cl) + have) if cl else None
                with path.open("ab" if have else "wb") as f:
                    while True:
                        chunk = r.read(1 << 18)
                        if not chunk:
                            break
                        f.write(chunk)
                        prog.add(len(chunk))
            if total is None or path.stat().st_size >= total:
                return
            raise RuntimeError(f"提前结束（{path.stat().st_size}/{total}）")
        except Exception as exc:  # noqa: BLE001
            if attempt > retries:
                raise SystemExit(f"✗ 下载失败（重试 {retries} 次）：{exc}")
            print(f"\n  ! 第 {attempt} 次出错：{exc}；断点续传重试")
            time.sleep(min(2 ** attempt, 30))


def _monitor(prog: _Progress, total: int | None, t0: float,
             stop: threading.Event) -> None:
    """每 0.5s 打一行聚合进度。分片模式下只有一个线程刷屏，不会互相打架。"""
    while not stop.wait(0.5):
        done = prog.get()
        el = max(time.time() - t0, 1e-6)
        spd = done / el
        left = (total - done) / spd if (total and spd > 0) else 0
        pct = f"{done * 100 / total:5.1f}%" if total else "  ?  "
        print(f"\r  {pct}  {human(done)}"
              f"{'/' + human(total) if total else ''}"
              f"  {human(spd)}/s  剩 {int(left)}s   ", end="", flush=True)


def _join(parts: list[Path], dest: Path) -> None:
    """按序拼接分片 → dest，并清掉临时分片。"""
    tmp = dest.with_name(dest.name + ".join")
    with tmp.open("wb") as out:
        for p in parts:
            with p.open("rb") as f:
                shutil.copyfileobj(f, out, 1 << 20)
    tmp.replace(dest)
    for p in parts:
        p.unlink(missing_ok=True)


def download_file(url: str, dest: Path, token: str, timeout: float,
                  retries: int = 5, parts: int = 4) -> None:
    """下载到 dest，默认 4 分片并发，各自断点续传，最后按序拼接。

    文件名统一为 `<dest>.partN`：分片数变化时，`.part0` 这类前缀相同的
    临时文件仍能各自续传（只要那个区间的起点没变）。
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.time()

    total, ranged = probe_total(url, token, timeout)
    if parts > 1 and not ranged:
        print("  · 服务端不支持 Range，退回单连接")
        parts = 1
    if total:
        # 每片至少 4 MiB，否则分片开销盖过收益
        parts = max(1, min(parts, -(-total // (4 << 20))))

    if parts <= 1:
        paths = [dest.with_name(dest.name + ".part0")]
        pre = min(paths[0].stat().st_size, total or 1 << 62) if paths[0].exists() else 0
        print(f"→ 单连接下载{f'（{human(total)}）' if total else ''}"
              f"{f'，续传 {human(pre)}' if pre else ''}")
        prog = _Progress(pre)
        stop = threading.Event()
        threading.Thread(target=_monitor, args=(prog, total, t0, stop),
                         daemon=True).start()
        try:
            _fetch_single(url, token, paths[0], timeout, retries, prog, total)
        finally:
            stop.set()
        _join(paths, dest)
        print(f"\r  完成 {human(dest.stat().st_size)}"
              f"（{time.time() - t0:.0f}s）" + " " * 24)
        return

    span = -(-total // parts)
    spans = [(i * span, min(total, (i + 1) * span) - 1) for i in range(parts)]
    paths = [dest.with_name(dest.name + f".part{i}") for i in range(parts)]
    pre = sum(min(p.stat().st_size, spans[i][1] - spans[i][0] + 1)
              for i, p in enumerate(paths) if p.exists())
    prog = _Progress(pre)

    print(f"→ {parts} 分片并发（每片约 {human(span)}，共 {human(total)}"
          f"{f'，已续传 {human(pre)}' if pre else ''}）")

    errors: list[BaseException] = []

    def guard(i: int) -> None:
        s, e = spans[i]
        try:
            _fetch_range(url, token, paths[i], s, e, timeout, retries, prog, i)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=guard, args=(i,), daemon=True)
               for i in range(parts)]
    stop = threading.Event()
    threading.Thread(target=_monitor, args=(prog, total, t0, stop),
                     daemon=True).start()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    stop.set()

    if errors:
        raise SystemExit(f"✗ 下载失败：{errors[0]}")

    _join(paths, dest)
    print(f"\r  完成 {human(dest.stat().st_size)}"
          f"（{time.time() - t0:.0f}s）" + " " * 24)


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for blk in iter(lambda: f.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()


def cmd_download(args) -> int:
    token = gh_token()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    art = api_get(f"/repos/{args.repo}/actions/artifacts/{args.artifact}", token)
    name, size, digest = art["name"], art["size_in_bytes"], art.get("digest")
    print(f"→ artifact {name}  {human(size)}")
    if art.get("expired"):
        print("✗ artifact 已过期（默认保留 30 天），需要重新触发构建")
        return 1

    zip_path = out_dir / f"{name}.zip"
    download_file(f"{API}/repos/{args.repo}/actions/artifacts/"
                  f"{args.artifact}/zip", zip_path, token, args.timeout,
                  parts=args.parts)

    if digest and digest.startswith("sha256:"):
        got = sha256_of(zip_path)
        if got == digest.split(":", 1)[1]:
            print(f"  ✓ zip sha256 与 API digest 一致：{got}")
        else:
            print(f"  ✗ zip sha256 不一致！\n    API: {digest}\n    实际: {got}")
            return 1

    # 解出内层 tar.gz（artifact zip 里就一个文件）
    with zipfile.ZipFile(zip_path) as z:
        members = [m for m in z.namelist() if not m.endswith("/")]
        print(f"→ zip 内条目：{members}")
        for m in members:
            target = out_dir / Path(m).name
            with z.open(m) as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst, 1 << 20)
            got = sha256_of(target)
            print(f"  ✓ {target.name}  {human(target.stat().st_size)}")
            print(f"    sha256 = {got}")
    return 0


# ───────────────────────────── 离线包结构校验 ─────────────────────────────

REQUIRED = [
    "hermes-offline-arm64/install.sh",
    "hermes-offline-arm64/check-env.sh",
    "hermes-offline-arm64/requirements.lock.txt",
    "hermes-offline-arm64/repo/hermes-agent-src.tar.gz",
    "hermes-offline-arm64/MANIFEST.sha256",
    "hermes-offline-arm64/build-info.json",
]


def cmd_verify(args) -> int:
    tb = Path(args.tarball)
    print(f"→ {tb.name}  {human(tb.stat().st_size)}")
    print(f"  sha256 = {sha256_of(tb)}")

    names: list[str] = []
    wheels: list[str] = []
    with tarfile.open(tb, "r:gz") as tf:
        for m in tf:
            names.append(m.name)
            if m.name.endswith(".whl"):
                wheels.append(m.name)

    print(f"  条目总数 = {len(names)}   wheel 数 = {len(wheels)}")

    missing = [r for r in REQUIRED if r not in names]
    if missing:
        print("  ✗ 缺少关键条目：")
        for m in missing:
            print(f"      {m}")
    else:
        print("  ✓ 6 个关键条目齐全")

    # wheel 架构/基线体检：非 aarch64 或 glibc 基线过新的要抓出来
    bad_arch, bad_glibc = [], []
    for w in wheels:
        base = os.path.basename(w)
        if "aarch64" not in base and "any" not in base and "none-any" not in base:
            bad_arch.append(base)
        m = re.search(r"manylinux_2_(\d+)_aarch64", base)
        if m and int(m.group(1)) > args.glibc_max:
            bad_glibc.append((base, int(m.group(1))))
    if bad_arch:
        print(f"  ✗ 非 aarch64 wheel {len(bad_arch)} 个：{bad_arch[:5]}")
    else:
        print("  ✓ 所有 wheel 都是 aarch64 / pure-python")
    if bad_glibc:
        print(f"  ✗ glibc 基线过高的 wheel {len(bad_glibc)} 个：")
        for b, v in bad_glibc[:10]:
            print(f"      manylinux_2_{v}  {b}")
    else:
        print(f"  ✓ 没有 glibc 基线 > 2.{args.glibc_max} 的 wheel")

    ok = not missing and not bad_arch and not bad_glibc
    print("  " + ("✅ 离线包校验通过" if ok else "❌ 离线包校验失败"))
    return 0 if ok else 1


# ───────────────────────────── CLI ─────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("runs", help="列最近的工作流运行")
    p.add_argument("--repo", required=True)
    p.add_argument("--limit", type=int, default=10)
    p.set_defaults(func=cmd_runs)

    p = sub.add_parser("watch", help="轮询某次运行直到结束")
    p.add_argument("--repo", required=True)
    p.add_argument("--run", type=int, required=True)
    p.add_argument("--interval", type=int, default=20)
    p.add_argument("--timeout", type=int, default=3600)
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("arts", help="列某次运行的 artifact")
    p.add_argument("--repo", required=True)
    p.add_argument("--run", type=int, required=True)
    p.set_defaults(func=cmd_arts)

    p = sub.add_parser("download",
                       help="下载 artifact（多分片并发+超时+续传+digest 校验）")
    p.add_argument("--repo", required=True)
    p.add_argument("--artifact", type=int, required=True)
    p.add_argument("--out", default="dist")
    p.add_argument("--timeout", type=float, default=60.0,
                   help="单次读操作的 socket 超时秒数")
    p.add_argument("--parts", type=int, default=4,
                   help="并发分片数（默认 4；服务端不支持 Range 时自动退回 1）")
    p.set_defaults(func=cmd_download)

    p = sub.add_parser("verify", help="校验离线包结构与 wheel 覆盖度")
    p.add_argument("--tarball", required=True)
    p.add_argument("--glibc-max", type=int, default=28,
                   help="允许的最大 glibc 基线次版本（2.28 → 28）")
    p.set_defaults(func=cmd_verify)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
