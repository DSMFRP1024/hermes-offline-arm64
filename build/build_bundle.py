#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Hermes Agent 离线包构建器 —— Linux ARM64 / aarch64
============================================================================

把 NousResearch/hermes-agent 变成一个**自包含的离线 ARM64 安装包**，
让目标机（信创 Kylin/UOS，glibc 2.28）在零网络下一条命令完成部署。

设计原则（务必先读）
--------------------
1. **原生模式（--native）是唯一模式**，且在 manylinux_2_28 容器内执行。
   runner 提供架构、容器提供 glibc 基线，两者缺一不可：
     ubuntu-24.04-arm runner 本体 glibc = 2.39 → 直接编译会产出
     "构建全绿、拷到目标机 GLIBC_2.39 not found" 的包。
   因为平台就是目标平台，PEP 508 标记天然正确，
   所以**不存在也不需要**证书劫持 / --platform / --python-version 那套交叉 hack。

2. 依赖清单**不重新求解**，而是从仓库自带的 uv.lock 导出。
   uv.lock 记录了每个包在所有平台上的 wheel URL + sha256，
   比任何二次求解都可靠；而 `uv export` 在原生平台上求值 marker 是正确的。

3. 失败必须可见：所有子进程输出同时进 stdout 与日志文件；
   判为未知失败时去掉 --quiet 重跑一次，把真实报错抓出来。

用法（CI 内部调用）
-------------------
    python build_bundle.py --native --repo /work/hermes-agent \
        --out /work/dist --node-line 24 --glibc 2.28

本地自检（Windows/macOS/x86 上只会跑参数不变量测试，不构建）
    python build_bundle.py --native --dry-run
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

HERMES_REPO = "https://github.com/NousResearch/hermes-agent.git"

# 固定 pin：构建产物必须可复现，不要用浮动分支
DEFAULT_HERMES_COMMIT = "d84ece48b8552501660be229797e2d2aa4cee8db"

# Node 版本线。install.sh 的 node_satisfies_build() 接受 22.22+ / 24.11+ / >=26，
# 且要避开 npm 11.10.0–11.16.x（该区间忽略 min-release-age-exclude，
# 会让 npm ci 在最新发布的依赖上 ETARGET 失败）。
# Node 26 在写这个脚本时 nodejs.org 只发 alpha（headers 404 → node-gyp 挂），
# 所以默认走 24 线，回退 22 线。
NODE_LINES = (24, 22)
NPM_BAD_MAJOR, NPM_BAD_MINOR_RANGE = 11, (10, 16)

# 支持的 glibc 基线（取 manylinux 的 minor 号）。
# 信创基线（Kylin V10 / UOS 20 / openEuler 20.03+）都是 2.28。
# 注意：值为 minor 号本身，不要写成 2028 —— 正则抓出来的也是 minor 号。
SUPPORTED_GLIBC_MINORS = (17, 24, 27, 28)

MAX_WHEEL_ROUNDS = 6
DOWNLOAD_RETRIES = 4

# GitHub 加速前缀（境内本机跑时用得上；CI 上直连即可）
GH_MIRRORS = ("https://ghfast.top/", "https://gh-proxy.com/", "https://ghproxy.net/")

GITHUB_API = "https://api.github.com"


# ---------------------------------------------------------------------------
# 日志 / 子进程
# ---------------------------------------------------------------------------

class Log:
    def __init__(self, path: Path | None):
        self.path = path
        self._fh = path.open("a", encoding="utf-8", newline="\n") if path else None

    def __call__(self, msg: str = "") -> None:
        line = str(msg)
        print(line, flush=True)
        if self._fh:
            self._fh.write(line + "\n")
            self._fh.flush()

    def close(self) -> None:
        if self._fh:
            self._fh.close()
            self._fh = None


LOG: Log = Log(None)


def run(cmd, cwd=None, env=None, check=True, log_tail=0, echo=True, timeout=None):
    """跑子进程，输出同时进 stdout 与日志文件。

    echo=False 时输出被捕获（用于轮询类命令），失败时自动打尾巴 ——
    绝不静默吞掉 pip 的报错文本（原生/交叉模式都吃过这个亏）。
    """
    pretty = " ".join(str(c) for c in cmd)
    if echo:
        LOG(f"$ {pretty}")
    merged = dict(os.environ)
    if env:
        merged.update(env)

    if echo:
        p = subprocess.run(cmd, cwd=cwd, env=merged, timeout=timeout)
        rc = p.returncode
    else:
        p = subprocess.run(cmd, cwd=cwd, env=merged, timeout=timeout,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        rc = p.returncode
        if rc != 0:
            LOG(f"  ! exit={rc}")
            for ln in (p.stdout or "").splitlines()[-max(log_tail, 40):]:
                LOG("    " + ln)

    if check and rc != 0:
        raise SystemExit(f"命令失败 (exit={rc}): {pretty}")
    return rc


# ---------------------------------------------------------------------------
# 网络
# ---------------------------------------------------------------------------

def _urlopen(url, timeout=60, headers=None):
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "hermes-offline-builder"})
    return urllib.request.urlopen(req, timeout=timeout)


def http_json(url, token=None):
    headers = {"User-Agent": "hermes-offline-builder", "Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    with _urlopen(url, timeout=60, headers=headers) as r:
        return json.loads(r.read().decode("utf-8"))


def download(url, dest: Path, mirrors=(), sha256=None, retries=DOWNLOAD_RETRIES) -> Path:
    """带重试 + 可选 sha256 校验的下载。已存在且校验通过则跳过（幂等，可续跑）。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 0:
        if sha256 is None or sha256_file(dest) == sha256:
            LOG(f"  ↷ 已存在，跳过: {dest.name}")
            return dest

    candidates = list(mirrors) + [""]
    last_err = None
    for attempt in range(1, retries + 1):
        for prefix in candidates:
            target = f"{prefix}{url}" if prefix else url
            try:
                LOG(f"  ↓ {dest.name}  (attempt {attempt}, via {'direct' if not prefix else prefix})")
                tmp = dest.with_suffix(dest.suffix + ".part")
                with _urlopen(target, timeout=180) as r, tmp.open("wb") as fh:
                    shutil.copyfileobj(r, fh, length=1 << 20)
                if sha256 and sha256_file(tmp) != sha256:
                    tmp.unlink(missing_ok=True)
                    raise RuntimeError("sha256 不匹配")
                tmp.replace(dest)
                LOG(f"  ✓ {dest.name}  ({dest.stat().st_size:,} B)")
                return dest
            except Exception as e:  # noqa: BLE001 - 逐个候选源尝试，最后统一报错
                last_err = e
                LOG(f"    × {type(e).__name__}: {e}")
        if attempt < retries:
            time.sleep(min(2 ** attempt, 15))
    raise SystemExit(f"下载失败: {url}\n  最后错误: {last_err}")


def sha256_file(path: Path, chunk=1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            b = fh.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def gh_release_asset(repo: str, pattern: str, token=None) -> tuple[str, str, int]:
    """在最近若干 release 里找第一个匹配的 asset，返回 (tag, 下载URL, 字节数)。"""
    releases = http_json(f"{GITHUB_API}/repos/{repo}/releases?per_page=8", token=token)
    rx = re.compile(pattern)
    for rel in releases:
        for asset in rel.get("assets", []):
            if rx.fullmatch(asset["name"]):
                return rel["tag_name"], asset["browser_download_url"], asset.get("size", 0)
    raise SystemExit(f"{repo}: 最近 8 个 release 里找不到匹配 {pattern!r} 的资源")


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

@dataclass
class Cfg:
    repo: Path
    out: Path
    python_version: str = "3.11"
    node_line: int = 24
    glibc: tuple = (2, 28)
    hermes_commit: str = DEFAULT_HERMES_COMMIT
    with_playwright: bool = True
    with_media: bool = True          # ripgrep / ffmpeg
    with_ftscjk: bool = True
    with_web_ui: bool = True        # dashboard 前端（离线包必须带，见 build_web_ui）
    pypi_index: str | None = None
    gh_mirror: bool = False
    token: str | None = None
    tarball: bool = False
    dry_run: bool = False
    log_dir: Path = field(default_factory=lambda: Path("logs"))


# ---------------------------------------------------------------------------
# 0. 前置自检
# ---------------------------------------------------------------------------

def preflight_native(cfg: Cfg) -> None:
    """--native 的硬门槛。

    不自检的最坏结果是产出一个"看起来成功、实际平台标签全错"的包，
    要等拷到离线机才暴露 —— 所以这里失败就立刻退出。
    """
    machine = platform.machine().lower()
    if machine not in ("aarch64", "arm64"):
        raise SystemExit(
            f"--native 要求 aarch64 宿主，当前是 {machine!r}。\n"
            "  原生模式必须在 ARM64 Linux 上跑（推荐 GitHub Actions:\n"
            "  runs-on: ubuntu-24.04-arm + quay.io/pypa/manylinux_2_28_aarch64 容器）。"
        )
    if sys.platform != "linux":
        raise SystemExit(f"--native 要求 Linux 宿主，当前是 {sys.platform!r}")

    cur = f"{sys.version_info.major}.{sys.version_info.minor}"
    if cur != cfg.python_version:
        raise SystemExit(
            f"--python 与当前解释器不一致：请求 {cfg.python_version}，实际 {cur}。\n"
            "  原生模式下 pip 按宿主解释器挑 wheel，两者不一致会挑错 abi 标签。\n"
            "  容器里请显式用 /opt/python/cp311-cp311/bin/python 运行本脚本。"
        )

    LOG("── 前置自检通过 ──")
    LOG(f"  架构      : {machine}")
    LOG(f"  解释器    : {sys.version.split()[0]} ({sys.executable})")
    LOG(f"  glibc     : {glibc_version()}")
    LOG(f"  目标基线  : manylinux_2_{cfg.glibc[1]} / python {cfg.python_version}")
    LOG()


def glibc_version() -> tuple:
    try:
        out = subprocess.run(["getconf", "GNU_LIBC_VERSION"], capture_output=True, text=True).stdout
        m = re.search(r"(\d+)\.(\d+)", out)
        if m:
            return int(m.group(1)), int(m.group(2))
    except Exception:  # noqa: BLE001
        pass
    return (0, 0)


def check_host_glibc_not_newer(cfg: Cfg) -> None:
    """容器 glibc 必须 <= 目标基线，否则编出来的 .so 会带更高的版本需求。

    manylinux_2_28 容器的 glibc 恰好是 2.28；若有人误在 runner 本体上跑，
    这里会拦住（runner 是 2.39）。
    """
    host = glibc_version()
    if host > cfg.glibc:
        raise SystemExit(
            f"宿主 glibc {host[0]}.{host[1]} 高于目标基线 {cfg.glibc[0]}.{cfg.glibc[1]}。\n"
            "  在这里编译原生扩展会链接到更高的 glibc，拷到目标机会 GLIBC_x.y not found。\n"
            "  请把构建放进 quay.io/pypa/manylinux_2_28_aarch64 容器。"
        )


# ---------------------------------------------------------------------------
# 1. 依赖闭包（从 uv.lock 导出，不重新求解）
# ---------------------------------------------------------------------------

def export_requirements(cfg: Cfg, uv_bin: Path, dest: Path) -> None:
    LOG("── [1/11] 导出依赖闭包（uv export --frozen）──")
    universal = dest / "requirements.universal.txt"
    run(
        [str(uv_bin), "export", "--frozen", "--no-emit-project",
         "--extra", "all", "--format", "requirements-txt",
         "--no-hashes", "--no-annotate", "--no-header",
         "--output-file", str(universal)],
        cwd=cfg.repo,
        env={"UV_NO_CONFIG": "1"},
    )

    # 原生平台上按当前 marker 求值，把闭包收敛成"目标机真正要装的那一份"。
    # pip 自己也会求值一次，但我们需要确定性的清单来做文件名清点。
    from pip._vendor.packaging.markers import default_environment
    from pip._vendor.packaging.requirements import Requirement

    env = dict(default_environment())
    applicable, skipped = [], 0
    for raw in universal.read_text(encoding="utf-8").splitlines():
        line = raw.split("#")[0].strip()
        if not line:
            continue
        try:
            req = Requirement(line)
        except Exception:  # noqa: BLE001
            applicable.append(line)
            continue
        if req.marker is None or req.marker.evaluate(env):
            applicable.append(line)
        else:
            skipped += 1

    # 目标机上 `pip install -e .` 用 --no-build-isolation，需要构建后端已在 venv 里。
    # setuptools 的版本必须与 pyproject.toml 的 [build-system].requires 一致。
    build_backend = ["setuptools==83.0.0", "wheel"]
    for spec in build_backend:
        if not any(spec.split("==")[0].lower() == a.split("==")[0].lower() for a in applicable):
            applicable.append(spec)

    lock = dest / "requirements.lock.txt"
    lock.write_text("\n".join(sorted(applicable, key=str.lower)) + "\n",
                    encoding="utf-8", newline="\n")

    LOG(f"  ✓ 目标平台适用的 pin: {len(applicable)} 条（跳过 {skipped} 条不适用 marker）")
    LOG(f"  → {lock}")
    LOG()


# ---------------------------------------------------------------------------
# 2. wheel
# ---------------------------------------------------------------------------

def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_lock(path: Path) -> list[tuple[str, str, str]]:
    """→ [(归一化名, 版本, 原始行)]"""
    out = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#")[0].strip()
        if not line or line.startswith("-"):
            continue
        m = re.match(r"^([A-Za-z0-9._-]+)\s*==\s*([^\s;]+)", line)
        if m:
            out.append((_norm(m.group(1)), m.group(2), line))
    return out


def inventory(*dirs: Path) -> set[tuple[str, str]]:
    """清点 wheel 文件名 → {(归一化名, 版本)}。

    判定依据是**文件名清点**，不是解析 pip 的报错文本 ——
    依赖图较深时 pip 只给笼统的 ResolutionImpossible，压根不点名是哪个包没有 wheel。
    """
    got = set()
    for d in dirs:
        if not d.exists():
            continue
        for f in d.glob("*.whl"):
            parts = f.name.split("-")
            if len(parts) >= 2:
                got.add((_norm(parts[0]), parts[1]))
    return got


def fetch_wheels(cfg: Cfg, lock: Path, wheels: Path, prebuilt: Path) -> None:
    LOG("── [2/11] 下载 wheel（aarch64 原生）──")
    wheels.mkdir(parents=True, exist_ok=True)
    prebuilt.mkdir(parents=True, exist_ok=True)

    pins = parse_lock(lock)
    index_args = ["--index-url", cfg.pypi_index] if cfg.pypi_index else []
    # pip 会拒绝 --platform/--python-version 与"允许 sdist"共存；
    # 这里不加任何平台限制 —— 宿主就是目标平台，这是原生模式的关键优势。
    base = [sys.executable, "-m", "pip", "download", "--no-deps", "--only-binary=:all:",
            *index_args]

    for rnd in range(1, MAX_WHEEL_ROUNDS + 1):
        LOG(f"  · 第 {rnd} 轮：批量下载")
        run([*base, "-r", str(lock), "-d", str(wheels)], check=False)

        have = inventory(wheels, prebuilt)
        missing = [p for p in pins if (p[0], p[1]) not in have]
        if not missing:
            LOG(f"  ✓ 闭包完整：{len(pins)} 个 pin 全部有 wheel")
            LOG()
            return

        LOG(f"  · 缺 {len(missing)} 个，逐个单包重试"
            f"（批量下载遇到第一个没 wheel 的包就中止，其余多半只是没来得及下）")
        still = []
        for name, ver, _ in missing:
            rc = run([*base, f"{name}=={ver}", "-d", str(wheels)],
                     check=False, echo=False, log_tail=8)
            if rc != 0 or (name, ver) not in inventory(wheels, prebuilt):
                still.append((name, ver))
        if not still:
            continue

        LOG(f"  · 确实只有 sdist 的包：{len(still)} 个 → 本地构建成 wheel")
        for name, ver in still:
            run([sys.executable, "-m", "pip", "wheel", "--no-deps",
                 *index_args, f"{name}=={ver}", "-w", str(prebuilt)],
                check=False, echo=False, log_tail=25)

    have = inventory(wheels, prebuilt)
    missing = [p for p in pins if (p[0], p[1]) not in have]
    if missing:
        listing = "\n".join(f"    - {n}=={v}" for n, v, _ in missing[:40])
        raise SystemExit(f"仍有 {len(missing)} 个 pin 拿不到 wheel，无法继续：\n{listing}")
    LOG(f"  ✓ 闭包完整：{len(pins)} 个 pin 全部有 wheel（含本地预构建）")
    LOG()


def audit_wheels(cfg: Cfg, wheels: Path, prebuilt: Path) -> None:
    """出厂体检：标签必须全是 aarch64/any，且没有混入 sdist。"""
    LOG("── wheel 标签审计 ──")
    bad_platform, bad_py, sdists = [], [], []
    # 与 manylinux 的 minor 号直接比。别用 GLIBC_TAGS 的 2014/2028 那套值 ——
    # 那是以 2.x 为单位的"年份式"标签，和正则抓到的 34 不同量纲，比了必错。
    max_minor = cfg.glibc[1]
    py_tag = "cp" + cfg.python_version.replace(".", "")

    for d in (wheels, prebuilt):
        if not d.exists():
            continue
        for f in sorted(d.iterdir()):
            # 注意 Path.suffix 对 "x.tar.gz" 只返回 ".gz"，必须整名匹配
            if f.name.endswith((".tar.gz", ".tgz", ".zip")):
                sdists.append(f.name)
                continue
            if f.suffix != ".whl":
                continue
            parts = f.stem.split("-")
            if len(parts) < 5:
                continue
            pytag, abitag, plattag = parts[-3], parts[-2], parts[-1]
            plats = plattag.split(".")
            if not any("aarch64" in p or p == "any" for p in plats):
                bad_platform.append(f.name)
            m = re.search(r"manylinux_2_(\d+)_aarch64", plattag)
            if m and int(m.group(1)) > max_minor:
                bad_platform.append(
                    f"{f.name}  (需要 glibc 2.{m.group(1)} > 基线 2.{max_minor})")
            # cpXXX 标签必须与目标一致；abi3 例外（向下兼容）
            if "any" not in plats:
                for tag in pytag.split("."):
                    if tag.startswith("cp") and tag != py_tag and not abitag.startswith("abi3"):
                        bad_py.append(f.name)
                        break

    if sdists:
        raise SystemExit(f"wheel 目录混入 sdist（离线安装阶段不能编译）: {sdists[:5]}")
    if bad_platform:
        raise SystemExit("平台标签有问题：\n" + "\n".join("  " + x for x in bad_platform[:20]))
    if bad_py:
        raise SystemExit("python 标签与目标解释器不一致：\n" + "\n".join("  " + x for x in bad_py[:20]))
    n = len(list(wheels.glob("*.whl"))) + len(list(prebuilt.glob("*.whl")))
    LOG(f"  ✓ {n} 个 wheel 标签全部合规（aarch64/any，glibc <= 2.{cfg.glibc[1]}）")
    LOG()


# ---------------------------------------------------------------------------
# 3. 运行时
# ---------------------------------------------------------------------------

def fetch_python_runtime(cfg: Cfg, runtime: Path) -> None:
    LOG("── [3/11] 独立 CPython 运行时（python-build-standalone）──")
    runtime.mkdir(parents=True, exist_ok=True)

    pattern = rf"cpython-{re.escape(cfg.python_version)}\.\d+\+\d+-aarch64-unknown-linux-gnu-install_only\.tar\.gz"
    tag, url, _size = gh_release_asset(
        "astral-sh/python-build-standalone", pattern, token=cfg.token)

    mirrors = GH_MIRRORS if cfg.gh_mirror else ()
    # 必须 unquote：PBS 的资产名里带 `+`（如 `3.11.16+20260901`），
    # release API 给的 browser_download_url 会把它编成 `%2B`。
    # 直接拿 URL 末段当文件名，落盘就是 `cpython-3.11.16%2B20260901-...tar.gz`
    # —— 装是能装（install.sh 用 glob 匹配），但文件名成了编码残渣，
    # 后面要按名字找这个文件时非常难查。
    dest = runtime / urllib.parse.unquote(url.rsplit("/", 1)[-1])
    download(url, dest, mirrors=mirrors)
    LOG(f"  ✓ 运行时 {dest.name}  (release {tag})")
    LOG()


def fetch_uv(cfg: Cfg, tools: Path) -> Path:
    LOG("── [4/11] uv（aarch64 二进制）──")
    tools.mkdir(parents=True, exist_ok=True)
    mirrors = GH_MIRRORS if cfg.gh_mirror else ()
    tag, url, _ = gh_release_asset("astral-sh/uv", r"uv-aarch64-unknown-linux-gnu\.tar\.gz",
                                   token=cfg.token)
    tgz = download(url, tools / "uv-aarch64-unknown-linux-gnu.tar.gz", mirrors=mirrors)
    with tarfile.open(tgz) as tf:
        for m in tf.getmembers():
            if m.name.endswith("/uv") or m.name == "uv":
                m.name = "uv"
                tf.extract(m, tools, filter="data")
                break
    uv_bin = tools / "uv"
    uv_bin.chmod(0o755)
    LOG(f"  ✓ uv {tag}")
    LOG()
    return uv_bin


def fetch_node(cfg: Cfg, runtime: Path) -> Path:
    LOG("── [5/11] Node.js（linux-arm64）──")
    idx = http_json("https://nodejs.org/dist/index.json")
    lines = [cfg.node_line] + [l for l in NODE_LINES if l != cfg.node_line]

    chosen = None
    for line in lines:
        min_minor = 11 if line == 24 else 22
        cands = [
            e for e in idx
            if e["version"].startswith(f"v{line}.")
            and "-" not in e["version"]
            and int(e["version"].split(".")[1]) >= min_minor
            and "linux-arm64" in (e.get("files") or [])
        ]
        cands.sort(key=lambda e: [int(x) for x in e["version"][1:].split(".")], reverse=True)
        for e in cands:
            npm = (e.get("npm") or "").lstrip("v")
            if npm_ok(npm):
                chosen = (e, npm)
                break
        if chosen:
            break

    if not chosen:
        raise SystemExit("找不到满足 engines 约束的 Node 版本（22.22+ / 24.11+，且 npm 不在 11.10-11.16）")

    entry, npm = chosen
    ver = entry["version"]
    name = f"node-{ver}-linux-arm64.tar.xz"
    url = f"https://nodejs.org/dist/{ver}/{name}"
    mirrors = GH_MIRRORS if cfg.gh_mirror else ()
    dest = download(url, runtime / name, mirrors=mirrors)
    (runtime / "NODE_CHOSEN.txt").write_text(f"{ver}\nnpm {npm}\n", encoding="utf-8")
    LOG(f"  ✓ Node {ver}（自带 npm {npm}，在 engines 安全区间内）")
    LOG()
    return dest


def npm_ok(ver: str) -> bool:
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)", ver or "")
    if not m:
        return False
    major, minor = int(m.group(1)), int(m.group(2))
    lo, hi = NPM_BAD_MINOR_RANGE
    return not (major == NPM_BAD_MAJOR and lo <= minor <= hi)


# ---------------------------------------------------------------------------
# 6. Node 依赖（必须在 arm64 上预构建：node-pty 没有 Linux 预编译包）
# ---------------------------------------------------------------------------

def node_env(cfg: Cfg, node_tgz: Path, work: Path) -> dict:
    """把 Node 解到 work/node 并返回环境变量。"""
    node_dir = work / "node"
    if not node_dir.exists():
        with tarfile.open(node_tgz) as tf:
            tf.extractall(work, filter="data")
        inner = next(work.glob("node-v*-linux-arm64"))
        inner.rename(node_dir)
    return {"PATH": f"{node_dir / 'bin'}:{os.environ.get('PATH', '')}"}


def fetch_node_modules(cfg: Cfg, env: dict, node_modules: Path) -> None:
    """预构建 node_modules。

    这一步必须放在 arm64 上做：node-pty 不发 Linux 预编译包，
    每次安装都要 node-gyp 编译，而信创机通常没有 make/gcc。
    在容器里编好再打包，目标机就只是解压 —— 彻底消灭编译器依赖。

    只装 CLI 实际需要的 workspace（ui-tui / web + root），
    故意排除 apps/*：那个 glob 会拉进 Electron + node-pty 的桌面链路，
    而 CLI 安装永远不会启动 Electron。
    """
    LOG("── [6/11] 预构建 node_modules（root + ui-tui + web）──")
    node_modules.mkdir(parents=True, exist_ok=True)
    repo = cfg.repo

    ws_args = []
    for ws in ("ui-tui", "web"):
        if (repo / ws / "package.json").exists():
            ws_args += ["--workspace", ws]
    ws_args += ["--include-workspace-root"] if ws_args else ["--workspaces=false"]

    # npm ci 按 lock 树安装，产物完整可复现；lock 不同步时回退 npm install。
    if run(["npm", "ci", *ws_args, "--no-audit", "--no-fund"],
           cwd=repo, env=env, check=False, echo=False, log_tail=30) != 0:
        LOG("  · npm ci 失败，回退 npm install")
        run(["npm", "install", *ws_args, "--no-audit", "--no-fund"],
            cwd=repo, env=env)

    if (repo / "ui-tui" / "package.json").exists():
        if run(["npm", "ci", "--no-audit", "--no-fund"], cwd=repo / "ui-tui",
               env=env, check=False, echo=False, log_tail=30) != 0:
            run(["npm", "install", "--no-audit", "--no-fund"], cwd=repo / "ui-tui", env=env)

    # 清掉跨平台误装的包（在不同架构宿主上跑过之后会留下），否则目标机会 import 到错的二进制
    n_removed = prune_foreign_native(repo)

    # 打包成 tarball（保留符号链接与可执行位）
    made = 0
    for rel in ("node_modules", "ui-tui/node_modules", "web/node_modules"):
        src = repo / rel
        if not src.is_dir():
            continue
        arc = node_modules / (rel.replace("/", "__") + ".tar.gz")
        with tarfile.open(arc, "w:gz", compresslevel=1) as tf:
            tf.add(src, arcname=rel)
        made += 1
        LOG(f"  ✓ {rel}  →  {arc.name}  ({arc.stat().st_size:,} B)")

    if not made:
        raise SystemExit("没有产出任何 node_modules，Node 依赖安装可能整体失败了")

    # node-pty 属于 apps/desktop，而 apps/* 是被刻意排除的（见上面 docstring），
    # 所以正常构建本来就**不该**出现它 —— 早先这里直接检查"有没有 .node"，
    # 结果是每轮构建都必然打一条"PTY 可能不可用"的警告，把"不在安装范围"
    # 误报成"构建失败"。真正该报警的是相反情形：它被拉进来了、却没编出来。
    nm_dirs = [repo / "node_modules", repo / "ui-tui/node_modules",
               repo / "web/node_modules"]
    present = [d for d in nm_dirs if (d / "node-pty").is_dir()]
    if not present:
        LOG("  · 未包含 node-pty（它在 apps/desktop 里，CLI 链路用不到，属预期）")
    # 注意要 any(list(...)) 而不是 any(gen for ...)：后者里每个元素是**生成器
    # 对象**，bool(生成器) 恒为真，条件会永远成立。
    elif any(list((d / "node-pty/build/Release").glob("*.node")) for d in present):
        LOG("  ✓ node-pty 原生模块已就绪")
    else:
        LOG("  ⚠ 拉进了 node-pty 却没编出 .node —— 终端模拟功能会在目标机不可用")
    LOG(f"  （已清理 {n_removed} 个非 linux-arm64 平台包）")
    LOG()


def prune_foreign_native(repo: Path) -> int:
    """删掉 node_modules 里明显是别的平台的产物，避免误带进包。"""
    suspects = re.compile(
        r"-(win32|darwin|freebsd|android)-(x64|arm64|ia32|arm)",
        re.IGNORECASE)
    removed = 0
    for nm in repo.glob("**/node_modules"):
        if ".git" in nm.parts:
            continue
        for child in nm.iterdir():
            try:
                if child.is_dir() and suspects.search(child.name):
                    shutil.rmtree(child, ignore_errors=True)
                    removed += 1
            except OSError:
                pass
    return removed


# ---------------------------------------------------------------------------
# 7. Web UI 前端（dashboard 的 React/Vite SPA，必须在构建机预编译）
# ---------------------------------------------------------------------------

def build_web_ui(cfg: Cfg, env: dict) -> None:
    """把 dashboard 前端编译好，让产物随仓库快照一起进包。

    为什么非在这里编不可 ——
    目标机是零网络信创机。上游 `hermes dashboard` 在找不到 dist 时会走
    `_do_build_web_ui()`：先 `npm install --prefer-offline`，再 `npm run build`。
    无外网时 npm install 基本必失败，随后 `_report_web_build_failure(..., fatal=True)`
    直接 `sys.exit(1)` —— 症状就是"包装好了，但控制台打不开"。

    所以必须在本机（有网的 arm64 容器）编好：
      - `vite.config.ts` 里写死 `outDir: "../hermes_cli/web_dist"`（相对 web/），
        产物因此落在仓库根的 `hermes_cli/web_dist`；
      - 安装是 editable 的（`pip install -e .`），运行时要找的
        `PROJECT_ROOT/hermes_cli/web_dist` 就是这个目录；
      - `web_dist` 不在 REPO_EXCLUDES 里，会被 pack_repo() 正常收进包。

    只放 dist 还不够：`_web_ui_build_needed()` 还会比对
    `$HERMES_HOME/web-ui-build-stamp.json` 里的内容哈希，而戳**不在仓库里**。
    那个戳由 target/install.sh 在目标机上写（见该脚本第 9 步）。
    """
    LOG("── [7/11] 预编译 Web UI（dashboard 前端）──")
    web_dir = cfg.repo / "web"
    dist = cfg.repo / "hermes_cli" / "web_dist"

    if not (web_dir / "package.json").exists():
        raise SystemExit(
            f"{web_dir} 下没有 package.json —— 无法预编译 dashboard 前端。"
            "上游若改了前端目录结构，这里要同步更新。")

    # 用 --workspace 从仓库根调用，避免 cwd 切换影响到 npm 的 workspace 解析
    # （web 的依赖被 hoist 到根 node_modules，cwd 不对就会找不到 vite/tsc）。
    rc = run(["npm", "run", "build", "--workspace", "web"],
             cwd=cfg.repo, env=env, check=False, echo=True, timeout=2400)
    if rc != 0:
        raise SystemExit(
            "Web UI 预编译失败（exit=%d）。目标机没有可用的 npm 环境，"
            "控制台会打不开 —— 这里不能放过，先修构建。" % rc)

    index = dist / "index.html"
    if not index.exists():
        # 真出现这种情况，多半是上游改了 vite 的 outDir。与其静默产出一个
        # "编了但运行时找不到"的包，不如当场失败并指出差异点。
        alt = web_dir / "dist" / "index.html"
        hint = f"（在 {alt} 发现了产物，说明上游改了 vite outDir）" if alt.exists() else ""
        raise SystemExit(
            f"预编译跑完了，但 {index} 不存在{hint}。"
            "上游 `_web_dist_dir()` 只认 hermes_cli/web_dist，请同步调整本函数。")

    files = [p for p in dist.rglob("*") if p.is_file()]
    size = sum(p.stat().st_size for p in files)
    assets = sum(1 for p in files if p.suffix in (".js", ".css"))
    if not assets:
        raise SystemExit(
            f"{dist} 里有 index.html 却没有 .js/.css 资源，产物不完整。")
    LOG(f"  ✓ hermes_cli/web_dist  ({len(files)} 个文件, {size / 1048576:.2f} MiB, "
        f"{assets} 个 js/css)")
    LOG("  · 目标机起 dashboard 不再需要 npm（install.sh 会补写 build stamp）")
    LOG()


# ---------------------------------------------------------------------------
# 8. Playwright Chromium（arm64）
# ---------------------------------------------------------------------------

def fetch_playwright(cfg: Cfg, env: dict, browsers: Path) -> None:
    """在 arm64 上把 Chromium 拉下来再打包。

    Playwright 确实发布 linux-arm64 的 Chromium 构建（Chrome 官方不发 arm64
    Linux 二进制），所以这一步在 arm64 上是能成的；目标是别让离线机去下载。

    注意：Chromium 运行时依赖一批系统库（libnss3/libgbm/libasound2/libdrm/
    libxkbcommon/libatk 等），信创机默认多数缺失 —— check-env.sh 会逐项列出，
    但二进制本身必须进包。
    """
    if not cfg.with_playwright:
        LOG("── [8/11] Playwright Chromium：已按参数跳过 ──\n")
        return

    LOG("── [8/11] Playwright Chromium（linux-arm64）──")
    browsers.mkdir(parents=True, exist_ok=True)
    stage = cfg.out / ".work" / "ms-playwright"
    stage.mkdir(parents=True, exist_ok=True)

    env = dict(env, PLAYWRIGHT_BROWSERS_PATH=str(stage))
    rc = run(["npx", "--yes", "playwright", "install", "chromium"],
             cwd=cfg.repo, env=env, check=False, echo=False, log_tail=40)
    if rc != 0:
        LOG("  ⚠ playwright install 失败 —— 浏览器工具在目标机上不可用，其余功能不受影响")

    dirs = [d for d in stage.iterdir() if d.is_dir()] if stage.exists() else []
    if not dirs:
        LOG("  ⚠ 浏览器缓存目录为空，跳过打包")
        LOG()
        return

    # 审计：确认拿到的是 ARM aarch64 原生二进制，不是 x86 模拟
    for d in dirs:
        exe = next((p for p in d.glob("chrome-linux*/chrome") if p.is_file()), None)
        if exe:
            head = exe.open("rb").read(20)
            arch = "aarch64" if b"\xb7" in head else "unknown"
            LOG(f"  · {d.name}: chrome ELF e_machine=0x{head[18]:02x}{head[19]:02x} ({arch})")

    arc = browsers / "ms-playwright-arm64.tar.gz"
    with tarfile.open(arc, "w:gz", compresslevel=1) as tf:
        for d in dirs:
            tf.add(d, arcname=d.name)
    LOG(f"  ✓ {arc.name}  ({arc.stat().st_size:,} B, {len(dirs)} 个浏览器目录)")
    LOG()


# ---------------------------------------------------------------------------
# 9. 命令行工具（ripgrep / ffmpeg）
# ---------------------------------------------------------------------------

def fetch_media_tools(cfg: Cfg, bin_dir: Path) -> None:
    """ripgrep 与 ffmpeg 的 aarch64 二进制。

    ffmpeg 用 johnvansickle 的**全静态**构建：BtbN 那套在 Ubuntu 22.04 上编译，
    带 glibc 2.35 依赖，拷到 2.28 的信创机会挂。
    """
    if not cfg.with_media:
        LOG("── [9/11] ripgrep/ffmpeg：已按参数跳过 ──\n")
        return

    LOG("── [9/11] ripgrep / ffmpeg（aarch64）──")
    bin_dir.mkdir(parents=True, exist_ok=True)
    mirrors = GH_MIRRORS if cfg.gh_mirror else ()

    # ripgrep：官方 release 里的 aarch64-unknown-linux-gnu（静态链接到 musl 变体更稳，
    # 但 gnu 变体在信创机上没问题，且体积小）
    try:
        tag, url, _ = gh_release_asset(
            "BurntSushi/ripgrep", r"ripgrep-\d+\.\d+\.\d+-aarch64-unknown-linux-gnu\.tar\.gz",
            token=cfg.token)
        tgz = download(url, bin_dir / "ripgrep.tar.gz", mirrors=mirrors)
        with tarfile.open(tgz) as tf:
            for m in tf.getmembers():
                if m.name.endswith("/rg"):
                    m.name = "rg"
                    tf.extract(m, bin_dir, filter="data")
        (bin_dir / "rg").chmod(0o755)
        (bin_dir / "ripgrep.tar.gz").unlink(missing_ok=True)
        LOG(f"  ✓ ripgrep {tag}")
    except SystemExit as e:
        LOG(f"  ⚠ ripgrep 打包失败（hermes 会回退到 grep）: {e}")

    # ffmpeg：best-effort，只影响 TTS 语音消息
    ff = bin_dir / "ffmpeg"
    try:
        if not ff.exists():
            tgz = download("https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-arm64-static.tar.xz",
                           bin_dir / "ffmpeg-static.tar.xz", mirrors=mirrors, retries=2)
            with tarfile.open(tgz) as tf:
                for m in tf.getmembers():
                    base = m.name.rsplit("/", 1)[-1]
                    if base in ("ffmpeg", "ffprobe") and m.isfile():
                        m.name = base
                        tf.extract(m, bin_dir, filter="data")
            for b in ("ffmpeg", "ffprobe"):
                if (bin_dir / b).exists():
                    (bin_dir / b).chmod(0o755)
            (bin_dir / "ffmpeg-static.tar.xz").unlink(missing_ok=True)
        LOG(f"  ✓ ffmpeg  {'已就绪' if ff.exists() else '缺失（不影响核心功能）'}")
    except SystemExit as e:
        LOG(f"  ⚠ ffmpeg 打包失败（只影响 TTS 语音消息）: {e}")
    LOG()


# ---------------------------------------------------------------------------
# 10. 原生扩展 fts5_cjk（CJK 分词，缺了会退化成 LIKE 全表扫）
# ---------------------------------------------------------------------------

def build_fts5_cjk(cfg: Cfg, out_dir: Path) -> None:
    if not cfg.with_ftscjk:
        LOG("── [10/11] fts5_cjk：已按参数跳过 ──\n")
        return
    LOG("── [10/11] 编译 fts5_cjk 原生扩展 ──")
    src = cfg.repo / "native" / "fts5_cjk"
    out_dir.mkdir(parents=True, exist_ok=True)
    if not (src / "build.sh").exists():
        LOG("  · 上游没有这个扩展，跳过\n")
        return
    so = next(iter(src.glob("*.so")), None)
    rc = run(["bash", "build.sh"], cwd=src, check=False, echo=False, log_tail=25)
    so = next(iter(src.glob("*.so")), so)
    if rc != 0 or not so:
        LOG("  ⚠ fts5_cjk 编译失败 —— 中文会话检索会退化成 LIKE 全表扫，功能可用但变慢\n")
        return
    shutil.copy2(so, out_dir / so.name)
    LOG(f"  ✓ {so.name}  ({so.stat().st_size:,} B)\n")


# ---------------------------------------------------------------------------
# 11. 源码树 + 清单
# ---------------------------------------------------------------------------

REPO_EXCLUDES = (".git", "website", "evals", "contributors")


def pack_repo(cfg: Cfg, dest: Path) -> None:
    LOG("── 打包源码树 ──")
    dest.parent.mkdir(parents=True, exist_ok=True)
    excludes = [f"--exclude={e}" for e in REPO_EXCLUDES]
    run(["tar", "-czf", str(dest), *excludes, "-C", str(cfg.repo.parent), cfg.repo.name])
    LOG(f"  ✓ {dest.name}  ({dest.stat().st_size:,} B)")
    LOG()


def write_manifest(cfg: Cfg, bundle: Path) -> None:
    LOG("── 生成清单 ──")
    lines = []
    for p in sorted(bundle.rglob("*")):
        if p.is_file() and p.name not in ("MANIFEST.sha256", "build-info.json"):
            lines.append(f"{sha256_file(p)}  {p.relative_to(bundle).as_posix()}")
    (bundle / "MANIFEST.sha256").write_text("\n".join(lines) + "\n",
                                           encoding="utf-8", newline="\n")

    info = {
        "bundle": "hermes-offline-arm64",
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "target": {
            "arch": "aarch64",
            "glibc_baseline": f"2.{cfg.glibc[1]}",
            "python": cfg.python_version,
            "node_line": cfg.node_line,
        },
        "source": {
            "repo": HERMES_REPO,
            "commit": cfg.hermes_commit,
        },
        "options": {
            "playwright": cfg.with_playwright,
            "media_tools": cfg.with_media,
            "fts5_cjk": cfg.with_ftscjk,
            "web_ui_prebuilt": cfg.with_web_ui,
        },
        "file_count": len(lines),
    }
    (bundle / "build-info.json").write_text(
        json.dumps(info, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    LOG(f"  ✓ {len(lines)} 个文件已入清单")
    LOG()


def assemble_bundle(cfg: Cfg, staging: Path, runtime: Path, wheels: Path,
                    prebuilt: Path, node_modules: Path, browsers: Path,
                    media: Path, lib: Path, repo_tgz: Path,
                    uv_bin: Path | None = None) -> Path:
    LOG("── 组装离线包 ──")
    bundle = staging / "hermes-offline-arm64"
    if bundle.exists():
        shutil.rmtree(bundle)
    bundle.mkdir(parents=True)

    for sub in ("repo", "runtime", "wheels", "wheels-prebuilt",
                "node_modules", "browsers", "bin", "lib"):
        (bundle / sub).mkdir()

    # runtime / wheels / node_modules / browsers / bin / lib：逐个文件拷
    for src_dir, sub in ((runtime, "runtime"), (wheels, "wheels"),
                         (prebuilt, "wheels-prebuilt"), (node_modules, "node_modules"),
                         (browsers, "browsers"), (media, "bin"), (lib, "lib")):
        if not src_dir or not src_dir.exists():
            continue
        for f in sorted(src_dir.iterdir()):
            if f.is_file():
                shutil.copy2(f, bundle / sub / f.name)

    # uv 是构建期工具，但它必须一起进包：目标机上 install.sh 会把它放到
    # $HERMES_HOME/bin/uv，让 Hermes 的"托管 uv"检测路径短路 —— 否则
    # `hermes tools` 之类会去联网下载 uv，在离线机上直接失败。
    # 只把 uv 放进构建机的 work/tools 而忘了拷进 bundle，症状是**静默**的：
    # install.sh 的 `if [ -f ] / elif [ -x ]` 两级都不成立，脚本不报错、
    # 什么都不做，直到用户跑起 CLI 才发现。
    if uv_bin and uv_bin.exists():
        dst = bundle / "bin" / "uv"
        shutil.copy2(uv_bin, dst)
        dst.chmod(0o755)
        LOG(f"  ✓ uv 入包 → bin/uv（{uv_bin.stat().st_size:,} B）")

    shutil.copy2(repo_tgz, bundle / "repo" / "hermes-agent-src.tar.gz")
    shutil.copy2(cfg.log_dir / "requirements.lock.txt", bundle / "requirements.lock.txt")
    shutil.copy2(cfg.log_dir / "requirements.universal.txt", bundle / "requirements.universal.txt")

    # 目标机脚本与文档
    project = Path(__file__).resolve().parent.parent
    for f in sorted((project / "target").iterdir()):
        if f.is_file():
            shutil.copy2(f, bundle / f.name)
    docs = project / "docs"
    if docs.is_dir():
        shutil.copytree(docs, bundle / "docs", dirs_exist_ok=True)

    n = sum(1 for _ in bundle.rglob("*"))
    size = sum(p.stat().st_size for p in bundle.rglob("*") if p.is_file())
    LOG(f"  ✓ {bundle}  ({n} 个条目, {size / 1e9:.2f} GB)")
    LOG()
    return bundle


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def build(cfg: Cfg) -> Path:
    preflight_native(cfg)
    check_host_glibc_not_newer(cfg)

    work = cfg.out / ".work"
    for d in (work, cfg.log_dir):
        d.mkdir(parents=True, exist_ok=True)

    if not (cfg.repo / "uv.lock").exists():
        raise SystemExit(f"{cfg.repo} 看起来不是 hermes-agent 检出（缺 uv.lock）")

    tools = work / "tools"
    uv_bin = fetch_uv(cfg, tools)
    export_requirements(cfg, uv_bin, cfg.log_dir)

    lock = cfg.log_dir / "requirements.lock.txt"
    wheels = work / "wheels"
    prebuilt = work / "wheels-prebuilt"
    fetch_wheels(cfg, lock, wheels, prebuilt)
    audit_wheels(cfg, wheels, prebuilt)

    runtime = work / "runtime"
    fetch_python_runtime(cfg, runtime)
    node_tgz = fetch_node(cfg, runtime)

    env = node_env(cfg, node_tgz, work)
    node_modules = work / "node_modules"
    fetch_node_modules(cfg, env, node_modules)

    # 必须排在 fetch_node_modules 之后（要用到它装出来的 vite/tsc），
    # 且必须排在 pack_repo 之前（产物要随源码快照一起入包）。
    if cfg.with_web_ui:
        build_web_ui(cfg, env)
    else:
        LOG("── [7/11] Web UI：已按 --skip-web-ui 跳过 ──")
        LOG("  ⚠ 目标机 `hermes dashboard` 将需要现场 npm 构建；离线环境下这是跑不通的")
        LOG()

    browsers = work / "browsers"
    fetch_playwright(cfg, env, browsers)

    media = work / "bin"
    fetch_media_tools(cfg, media)

    lib = work / "lib"
    build_fts5_cjk(cfg, lib)

    repo_tgz = work / "hermes-agent-src.tar.gz"
    pack_repo(cfg, repo_tgz)

    bundle = assemble_bundle(cfg, cfg.out, runtime, wheels, prebuilt,
                             node_modules, browsers, media, lib, repo_tgz,
                             uv_bin=uv_bin)
    write_manifest(cfg, bundle)

    if cfg.tarball:
        # 用 gzip -1 而不是默认的 -6：这是个 1GB+ 的包，压缩级别对体积影响
        # 有限，但对时间影响很大（arm64 双核上 -6 要多花好几分钟）。
        # 上传时 artifact 会再压一遍，所以不值得在这里追求极限压缩率。
        LOG("── 打包 tar.gz（gzip -1，快速压缩）──")
        run(["tar", "-I", "gzip -1", "-cf", str(cfg.out / "hermes-offline-arm64.tar.gz"),
             "-C", str(cfg.out), "hermes-offline-arm64"])
        LOG()

    LOG("=" * 66)
    LOG(f"✅ 构建完成: {bundle}")
    LOG("=" * 66)
    return bundle


def make_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="构建 Hermes Agent 的离线 ARM64 安装包",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--native", action="store_true", required=True,
                   help="原生模式（必须在 aarch64 Linux / manylinux 容器内执行）")
    p.add_argument("--repo", type=Path, default=Path("hermes-agent"),
                   help="hermes-agent 检出目录")
    p.add_argument("--out", type=Path, default=Path("dist"), help="产物目录")
    p.add_argument("--python", dest="python_version", default="3.11",
                   help="目标 Python 版本（默认 3.11，与上游 .python-version 一致）")
    p.add_argument("--node-line", type=int, default=24, help="Node 主版本线（默认 24）")
    p.add_argument("--glibc", default="2.28", help="目标 glibc 基线（默认 2.28）")
    p.add_argument("--hermes-commit", default=DEFAULT_HERMES_COMMIT)
    p.add_argument("--index-url", dest="pypi_index", default=None,
                   help="PyPI 镜像（如 https://pypi.tuna.tsinghua.edu.cn/simple）")
    p.add_argument("--gh-mirror", action="store_true",
                   help="GitHub 资源走加速前缀（境内本机构建用）")
    p.add_argument("--skip-playwright", action="store_true")
    p.add_argument("--skip-media", action="store_true")
    p.add_argument("--skip-fts5-cjk", action="store_true")
    p.add_argument("--skip-web-ui", action="store_true",
                   help="不预编译 dashboard 前端。**仅用于本地快速迭代**："
                        "离线目标机没有 npm 环境，跳过后控制台打不开。")
    p.add_argument("--tarball", action="store_true", help="额外产出 tar.gz")
    p.add_argument("--dry-run", action="store_true",
                   help="只跑前置自检与参数不变量，不真正构建")
    return p


def main(argv=None) -> int:
    args = make_argparser().parse_args(argv)

    major, minor = (args.glibc.split(".") + ["0"])[:2]
    if (int(major), int(minor)) != (2, int(minor)):
        raise SystemExit(f"--glibc 只支持 2.x 基線，收到 {args.glibc!r}")
    if int(minor) not in SUPPORTED_GLIBC_MINORS:
        raise SystemExit(
            f"--glibc 2.{minor} 不是受支持的 manylinux 基线，"
            f"可选: {', '.join('2.' + str(m) for m in SUPPORTED_GLIBC_MINORS)}")
    cfg = Cfg(
        repo=args.repo.resolve(),
        out=args.out.resolve(),
        python_version=args.python_version,
        node_line=args.node_line,
        glibc=(int(major), int(minor)),
        hermes_commit=args.hermes_commit,
        with_playwright=not args.skip_playwright,
        with_media=not args.skip_media,
        with_ftscjk=not args.skip_fts5_cjk,
        with_web_ui=not args.skip_web_ui,
        pypi_index=args.pypi_index,
        gh_mirror=args.gh_mirror,
        token=os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN"),
        tarball=args.tarball,
        dry_run=args.dry_run,
        log_dir=args.out.resolve() / "logs",
    )
    cfg.log_dir.mkdir(parents=True, exist_ok=True)

    global LOG
    LOG = Log(cfg.log_dir / "build.log")
    try:
        LOG("=" * 66)
        LOG("  Hermes Agent 离线包构建器（ARM64 / aarch64）")
        LOG("=" * 66)
        if glibc_version() < (2, 28):
            LOG("⚠ 宿主 glibc 低于 2.28，很多现代 wheel 会不可用")
        if cfg.dry_run:
            preflight_native(cfg)
            LOG("dry-run：仅完成前置自检")
            return 0
        build(cfg)
        return 0
    finally:
        LOG.close()


if __name__ == "__main__":
    sys.exit(main())
