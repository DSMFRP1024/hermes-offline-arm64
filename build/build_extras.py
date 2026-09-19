#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Hermes 离线增强包 —— 构建器。

两个阶段，和主包 build_bundle.py 一样是**分离**的：

    wheels  在 manylinux_2_28_aarch64 容器里跑。容器提供 glibc 2.28 基线与
            cp311，所以 pip 求值出来的轮子天然就是目标平台要的那一份 ——
            不需要 --platform / --python-version 那套交叉 hack（那套会按宿主
            求值 PEP 508 标记，linux-only 的依赖会被静默漏掉）。

    pack    在 runner 上跑。把 wheels / mcp-wheels / mcp-node / 脚本 / 清单一并
            组装成 hermes-extras-offline-arm64.tar.gz。

为什么增强包要独立成包、而不是并进主包：
    主包 941 MB（含 Electron 桌面版 + Chromium），改一行都要重跑整条 CI 再重下
    一整个包。增强包只有几百 MB、构建几分钟，可以反复重跑、也可以卸载。

用法：
    python3 build/build_extras.py wheels --out dist/hermes-extras --python ...
    python3 build/build_extras.py pack   --src dist/hermes-extras \\
        --node-src dist/hermes-extras-mcp-node --out dist/hermes-extras-offline-arm64
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXTRAS = ROOT / "extras"

PKG_DIR = "hermes-extras-offline-arm64"

# 只有 sdist 的包：主下载用 `--only-binary=:all:`，碰上它们会直接报错，
# 所以单独挑出来用容器里的 gcc/刚需工具链编成纯 Python 轮子。
# odfpy 是纯 Python，编出来就是 py3-none-any，目标机不需要编译器。
SDIST_PKGS = {"odfpy"}

# 有 aarch64 轮子的底线：文件名必须以这两个后缀之一结尾。
ARM_SUFFIX = "_aarch64.whl"
NOARCH_SUFFIXES = ("-none-any.whl",)

# manylinux 标签 → glibc minor。manylinux2014 == 2.17，manylinux1 == 2.5。
_LEGACY_MANYLINUX = {"manylinux1": 5, "manylinux2010": 12, "manylinux2014": 17}


# ── 小工具 ────────────────────────────────────────────────────────────────

def log(msg: str) -> None:
    print(msg, flush=True)


def die(msg: str) -> None:
    print(f"✗ {msg}", file=sys.stderr, flush=True)
    sys.exit(1)


def hr(title: str) -> None:
    log("")
    log(f"── {title} ──")


def sh(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    """跑命令；失败时把 stdout/stderr 一起抛出（CI 日志里才看得到原因）。"""
    log("  $ " + " ".join(str(c) for c in cmd))
    p = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", **kw)
    if p.returncode != 0:
        log(p.stdout or "")
        log(p.stderr or "")
        raise RuntimeError(f"命令失败（退出码 {p.returncode}）：{' '.join(map(str, cmd))}")
    return p


# ── requirements 解析 ─────────────────────────────────────────────────────

def parse_reqs(path: Path) -> list[str]:
    """读 requirements 文件，返回**顶层包名**列表（去掉 extras / 版本 / 注释）。

    只支持本项目实际用到的语法子集：一行一个包，行首 `#` 是注释，允许 `pkg[extra]`。
    """
    names: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = line.split("#", 1)[0].strip() if " #" in line else line
        if not line or line.startswith("-"):
            continue
        head = re.split(r"[\[<>=!~;\s]", line, maxsplit=1)[0].strip()
        if head:
            names.append(head)
    # 去重且保序
    seen: set[str] = set()
    out = []
    for n in names:
        if n.lower() not in seen:
            seen.add(n.lower())
            out.append(n)
    return out


def build_req_files(lean: bool) -> tuple[list[str], list[str]]:
    """返回 (增强包顶层包名, MCP venv 顶层包名)。"""
    extras_reqs = parse_reqs(EXTRAS / "requirements-extras.txt")
    if not lean:
        extras_reqs += parse_reqs(EXTRAS / "requirements-extras-heavy.txt")
    mcp_reqs = parse_reqs(EXTRAS / "requirements-mcp.txt")
    return extras_reqs, mcp_reqs


# ── 轮子元数据 / 审计 ─────────────────────────────────────────────────────

def wheel_meta(path: Path) -> tuple[str, str]:
    """从 wheel 的 .dist-info/METADATA 里读 (Name, Version)。

    不靠文件名解析：文件名里的名字是 replace('-','_') 过的，版本也可能带 build tag，
    而 METADATA 是权威值。
    """
    with zipfile.ZipFile(path) as zf:
        for n in zf.namelist():
            if n.endswith(".dist-info/METADATA"):
                name = version = ""
                with zf.open(n) as fh:
                    for line in fh.read().decode("utf-8", "replace").splitlines():
                        if not line:
                            break  # 头部到第一个空行为止
                        if line.startswith("Name: "):
                            name = line[6:].strip()
                        elif line.startswith("Version: "):
                            version = line[9:].strip()
                if name and version:
                    return name, version
    raise RuntimeError(f"读不出 METADATA：{path.name}")


def wheel_glibc_minor(name: str) -> int | None:
    """wheel 平台标签要求的 glibc minor；平台无关轮子返回 None。"""
    low = name.lower()
    if low.endswith("-none-any.whl"):
        return None
    m = re.search(r"manylinux_2_(\d+)_", low)
    if m:
        return int(m.group(1))
    for tag, minor in _LEGACY_MANYLINUX.items():
        if f"{tag}_" in low:
            return minor
    return None


def audit_wheels(wheels: list[Path], glibc_minor: int) -> list[str]:
    """返回问题列表（空 = 通过）。

    这是「构建全绿、拷到信创机 GLIBC_2.xx not found / 找不到匹配分发」唯一能
    提前拦下来的一道闸。轮子标签是构建环境的事实记录。
    """
    bad: list[str] = []
    for w in wheels:
        n = w.name
        low = n.lower()
        if not (low.endswith(ARM_SUFFIX) or any(low.endswith(s) for s in NOARCH_SUFFIXES)):
            bad.append(f"非 aarch64 轮子：{n}")
            continue
        if "musllinux" in low:
            bad.append(f"musl 轮子（glibc 机加载不了）：{n}")
            continue
        minor = wheel_glibc_minor(n)
        if minor is not None and minor > glibc_minor:
            bad.append(f"glibc 基线过高 manylinux_2_{minor} > 2.{glibc_minor}：{n}")
    return bad


def write_lock(wheels: list[Path], dest: Path) -> list[tuple[str, str]]:
    """按 wheel 元数据生成 `name==version` 锁文件（按名字排序，可 diff）。"""
    pairs = sorted({wheel_meta(w) for w in wheels}, key=lambda p: p[0].lower())
    dest.write_text("\n".join(f"{n}=={v}" for n, v in pairs) + "\n", encoding="utf-8")
    return pairs


# ── 阶段 1：wheels ────────────────────────────────────────────────────────

def _pip_download(python: str, dest: Path, reqs: list[str], index: str | None) -> None:
    """一条命令装齐一组顶层包的全部依赖闭包。

    --only-binary=:all: 是刻意的：目标机没有编译器，任何 sdist 都会在装机时
    变成"装不上"。宁可在这里失败，也不要在信创机上失败。
    """
    if not reqs:
        return
    cmd = [python, "-m", "pip", "download", "--only-binary=:all:",
           "--dest", str(dest), "--progress-bar", "off"]
    if index:
        cmd += ["--index-url", index]
    cmd += reqs
    sh(cmd)


def stage_wheels(args: argparse.Namespace) -> int:
    out = Path(args.out)
    wheels_dir = out / "wheels"
    mcp_dir = out / "mcp-wheels"
    for d in (wheels_dir, mcp_dir):
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True)

    extras_reqs, mcp_reqs = build_req_files(args.lean)
    glibc_minor = int(str(args.glibc).split(".")[-1])

    dropped: list[str] = []
    notes: dict[str, object] = {}

    # ── 1) hermes venv 那一组 ──
    hr(f"下载增强包依赖（{len(extras_reqs)} 个顶层包）")
    plain = [r for r in extras_reqs if r.lower() not in SDIST_PKGS]
    _pip_download(args.python, wheels_dir, plain, args.index)

    sdist_have = [r for r in extras_reqs if r.lower() in SDIST_PKGS]
    if sdist_have:
        # 容器里有 gcc，纯 Python 的 sdist 直接编成 py3-none-any 轮子。
        # 刻意**不加 --no-deps**：依赖（如 odfpy 的 defusedxml）必须一起落进
        # wheelhouse，否则目标机上 `pip install --no-index` 会因为依赖缺失而失败 ——
        # 而那是个"构建全绿、装机才炸"的坑。
        for pkg in sdist_have:
            hr(f"从 sdist 编轮子：{pkg}")
            sh([args.python, "-m", "pip", "wheel",
                "--wheel-dir", str(wheels_dir), "--progress-bar", "off", pkg])

    # ── 2) MCP venv 那一组（独立 venv，见 requirements-mcp.txt 的说明）──
    hr(f"下载 MCP 依赖（{len(mcp_reqs)} 个顶层包）")
    _pip_download(args.python, mcp_dir, mcp_reqs, args.index)

    # ── 3) 审计 ──
    hr("审计轮子")
    ex_wheels = sorted(wheels_dir.glob("*.whl"))
    mcp_wheels = sorted(mcp_dir.glob("*.whl"))
    if not ex_wheels:
        die("一个增强包轮子都没下到")
    if not mcp_wheels:
        die("一个 MCP 轮子都没下到")

    problems = audit_wheels(ex_wheels, glibc_minor) + audit_wheels(mcp_wheels, glibc_minor)
    if problems:
        for p in problems:
            log(f"  ✗ {p}")
        die(f"{len(problems)} 个轮子不合格")
    log(f"  ✓ {len(ex_wheels) + len(mcp_wheels)} 个轮子全部 aarch64/py3-none-any，"
        f"且 glibc 基线 ≤ 2.{glibc_minor}")

    # ── 4) 锁文件 ──
    hr("生成锁文件")
    ex_pairs = write_lock(ex_wheels, out / "requirements-extras.lock.txt")
    mcp_pairs = write_lock(mcp_wheels, out / "requirements-mcp.lock.txt")
    log(f"  增强包 {len(ex_pairs)} 个包 → requirements-extras.lock.txt")
    log(f"  MCP    {len(mcp_pairs)} 个包 → requirements-mcp.lock.txt")

    # ── 5) 顶层包是否真的都拿到了 ──
    have = {n.lower().replace("_", "-") for n, _ in ex_pairs}
    missing = [r for r in extras_reqs if r.lower().replace("_", "-") not in have]
    if missing:
        # 顶层包缺失说明解析被静默降级了（比如某个包名根本不存在），必须报错。
        die(f"以下顶层包没出现在解析结果里：{missing}")
    have_mcp = {n.lower().replace("_", "-") for n, _ in mcp_pairs}
    missing_mcp = [r for r in mcp_reqs if r.lower().replace("_", "-") not in have_mcp]
    if missing_mcp:
        die(f"以下 MCP 顶层包没出现在解析结果里：{missing_mcp}")

    # ── 6) MCP 服务器清单（哪些真的装得上，install 侧只认这份）──
    hr("登记 MCP 服务器")
    manifest = mcp_manifest()
    available = []
    for entry in manifest["python"]:
        if entry["package"].lower().replace("_", "-") in have_mcp:
            available.append(entry)
        else:
            dropped.append(entry["package"])
            log(f"  ! 跳过（轮子缺失）：{entry['package']}")
    manifest["python"] = available
    notes["dropped_servers"] = dropped
    (out / "mcp-servers.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    log(f"  Python 侧 MCP server：{len(available)}/{len(manifest['python']) + len(dropped)} 可用")

    # ── 7) 体积归因（别靠猜）──
    hr("体积")
    total = 0
    for d in (wheels_dir, mcp_dir):
        size = sum(f.stat().st_size for f in d.glob("*.whl"))
        total += size
        log(f"  {d.name:14s} {len(list(d.glob('*.whl'))):4d} 个  {size / 1048576:8.1f} MiB")
    log(f"  {'合计':14s} {total / 1048576:8.1f} MiB")
    log("  · 最大的 8 个轮子：")
    allw = ex_wheels + mcp_wheels
    for w in sorted(allw, key=lambda p: p.stat().st_size, reverse=True)[:8]:
        log(f"      {w.stat().st_size / 1048576:7.1f} MiB  {w.name}")

    summary = {
        "stage": "wheels",
        "python": args.python,
        "glibc": args.glibc,
        "lean": bool(args.lean),
        "extras_wheels": len(ex_wheels),
        "mcp_wheels": len(mcp_wheels),
        "extras_packages": len(ex_pairs),
        "mcp_packages": len(mcp_pairs),
        "wheel_bytes": total,
        "dropped_servers": dropped,
        "key_versions": {n: v for n, v in ex_pairs
                         if n.lower() in {"pandas", "numpy", "matplotlib", "pillow",
                                          "python-pptx", "openpyxl", "pymupdf"}},
    }
    (out / "wheels-stage.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    log("")
    log("✓ wheels 阶段完成")
    return 0


# ── MCP 服务器清单（源文件 extras/mcp-servers.json）───────────────────────

def mcp_manifest() -> dict:
    data = json.loads((EXTRAS / "mcp-servers.json").read_text(encoding="utf-8"))
    # 深拷贝，免得调用方改到缓存
    return json.loads(json.dumps(data))


def resolve_node_entry(root: Path, pkg: str) -> tuple[Path, str]:
    """给定 node_modules 根与包名，解析出入口脚本的绝对路径与包版本。

    从 package.json 的 `bin` 字段解析，而不是把 dist/index.js 写死 ——
    上游改目录结构时不至于静默失效。
    """
    pkg_dir = root / "node_modules" / Path(*pkg.split("/"))
    pj = pkg_dir / "package.json"
    if not pj.is_file():
        raise RuntimeError(f"node_modules 里没有这个包：{pkg}")
    meta = json.loads(pj.read_text(encoding="utf-8"))
    bins = meta.get("bin")
    if isinstance(bins, str):
        entry = bins
    elif isinstance(bins, dict) and bins:
        # 优先取与包名同名的入口，否则取第一个（字典序，保证可复现）
        short = pkg.split("/")[-1]
        entry = bins.get(short) or bins[sorted(bins)[0]]
    else:
        entry = meta.get("main") or "dist/index.js"
    path = (pkg_dir / entry).resolve()
    if not path.is_file():
        raise RuntimeError(f"{pkg} 的入口不存在：{path}")
    return path, str(meta.get("version", ""))


def stage_pack(args: argparse.Namespace) -> int:
    src = Path(args.src)
    node_src = Path(args.node_src) if args.node_src else None
    dest = Path(args.out)

    for req in ("wheels", "mcp-wheels", "requirements-extras.lock.txt",
                "requirements-mcp.lock.txt", "mcp-servers.json"):
        if not (src / req).exists():
            die(f"wheels 阶段没产出 {req}（先跑 wheels 阶段）")

    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)

    # ── 1) 固定文件 ──
    hr("组装固定文件")
    for f in ("install-extras.sh", "verify-mcp.py", "merge-mcp-config.py", "README.md",
              "requirements-extras.txt", "requirements-extras-heavy.txt",
              "requirements-mcp.txt", "skills-official.txt"):
        shutil.copy2(EXTRAS / f, dest / f)
    for f in ("requirements-extras.lock.txt", "requirements-mcp.lock.txt",
              "mcp-servers.json"):
        shutil.copy2(src / f, dest / f)
    log(f"  ✓ {len(list(dest.iterdir()))} 个文件")

    # ── 2) 轮子 ──
    hr("复制轮子")
    for sub in ("wheels", "mcp-wheels"):
        shutil.copytree(src / sub, dest / sub)
        n = len(list((dest / sub).glob("*.whl")))
        log(f"  {sub}: {n} 个")

    # ── 3) Node 侧 MCP 服务器 ──
    hr("登记 Node 侧 MCP 服务器")
    manifest = json.loads((dest / "mcp-servers.json").read_text(encoding="utf-8"))
    manifest["build"] = json.loads((src / "wheels-stage.json").read_text(encoding="utf-8")) \
        if (src / "wheels-stage.json").exists() else {}

    node_entries = []
    if node_src and (node_src / "node_modules").is_dir():
        node_dest = dest / "mcp-node"
        node_dest.mkdir(parents=True, exist_ok=True)
        shutil.copy2(node_src / "package.json", node_dest / "package.json")
        log("  复制 node_modules（这是最慢的一步）...")
        shutil.copytree(node_src / "node_modules", node_dest / "node_modules",
                        symlinks=True)
        for entry in manifest.get("node", []):
            try:
                path, ver = resolve_node_entry(node_dest, entry["package"])
            except RuntimeError as e:
                log(f"  ✗ {entry['package']}：{e}")
                die("Node MCP 服务器缺失 —— npm 安装步骤有问题")
            rel = path.relative_to(node_dest).as_posix()
            node_entries.append({**entry, "entry": rel, "version": ver})
            log(f"  ✓ {entry['name']:20s} {entry['package']}@{ver} → {rel}")
    else:
        log("  ! 没有提供 --node-src，Node 侧 MCP 服务器将缺席")
    manifest["node"] = node_entries
    (dest / "mcp-servers.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # ── 4) 清单 ──
    hr("生成 MANIFEST.sha256")
    lines = []
    for p in sorted(dest.rglob("*")):
        if p.is_file() and p.name != "MANIFEST.sha256":
            h = hashlib.sha256(p.read_bytes()).hexdigest()
            lines.append(f"{h}  {p.relative_to(dest).as_posix()}")
    (dest / "MANIFEST.sha256").write_text("\n".join(lines) + "\n", encoding="utf-8")
    log(f"  ✓ {len(lines)} 个文件已登记")

    # ── 5) build-info.json ──
    build = manifest.get("build", {})
    info = {
        "built_at": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "package": PKG_DIR,
        "target": "linux/aarch64, CPython 3.11, glibc 2.28+",
        "lean": build.get("lean", False),
        "manifest_files": len(lines),
        "extras_wheels": len(list((dest / "wheels").glob("*.whl"))),
        "mcp_wheels": len(list((dest / "mcp-wheels").glob("*.whl"))),
        "mcp_python_servers": [e["name"] for e in manifest["python"]],
        "mcp_node_servers": [e["name"] for e in manifest["node"]],
        "dropped_servers": build.get("dropped_servers", []),
        "key_versions": build.get("key_versions", {}),
    }
    (dest / "build-info.json").write_text(
        json.dumps(info, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    log(f"  · MCP python: {', '.join(info['mcp_python_servers']) or '(none)'}")
    log(f"  · MCP node  : {', '.join(info['mcp_node_servers']) or '(none)'}")

    # ── 6) 打 tar ──
    tarball = Path(args.tarball)
    tarball.parent.mkdir(parents=True, exist_ok=True)
    if tarball.exists():
        tarball.unlink()
    hr(f"打包 {tarball.name}")
    counter = {"n": 0}

    def _filter(ti: tarfile.TarInfo) -> tarfile.TarInfo:
        counter["n"] += 1
        # 目标机的解压用户不一定是打包机上的用户；统一成 755/644，
        # 免得出现 "解压后 install-extras.sh 不可执行"。
        ti.mode = 0o755 if (ti.isdir() or ti.name.endswith(".sh")) else 0o644
        ti.uid = ti.gid = 0
        ti.uname = ti.gname = "root"
        return ti

    with tarfile.open(tarball, "w:gz", compresslevel=6) as tf:
        tf.add(dest, arcname=PKG_DIR, filter=_filter)
    size = tarball.stat().st_size
    log(f"  ✓ {counter['n']} 个条目，{size / 1048576:.1f} MiB")
    log(f"  sha256 {hashlib.sha256(tarball.read_bytes()).hexdigest()}")
    log("")
    log("✓ pack 阶段完成")
    return 0


# ── CLI ───────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Hermes 离线增强包构建器")
    sub = ap.add_subparsers(dest="stage", required=True)

    w = sub.add_parser("wheels", help="容器内：下载并审计轮子")
    w.add_argument("--out", required=True, help="中间产物目录，如 dist/hermes-extras")
    w.add_argument("--python", default=sys.executable)
    w.add_argument("--glibc", default="2.28")
    w.add_argument("--index", default="", help="PyPI 镜像 URL（留空 = 官方源）")
    w.add_argument("--lean", action="store_true", help="跳过 heavy 组（opencv/jupyter 等）")
    w.set_defaults(func=stage_wheels)

    p = sub.add_parser("pack", help="runner 上：组装 tar.gz")
    p.add_argument("--src", required=True)
    p.add_argument("--node-src", default="")
    p.add_argument("--out", required=True)
    p.add_argument("--tarball", required=True)
    p.set_defaults(func=stage_pack)

    args = ap.parse_args(argv)
    args.index = args.index or None
    return args.func(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as exc:  # 子进程失败的统一出口
        die(str(exc))
    except KeyboardInterrupt:
        sys.exit(130)
