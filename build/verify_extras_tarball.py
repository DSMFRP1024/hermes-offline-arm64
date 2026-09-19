#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对增强包 tarball 做交付前体检（流式读 tar，不解包、不联网）。

这个脚本是**产物侧**的唯一权威检查：CI 打完包立刻跑一遍，
本地下载回来再跑一遍，两边用同一份规则，避免"CI 说没问题、交付前发现不对劲"。

它拦过的真实问题：
  · MANIFEST 把 npm 的符号链接按"目标内容"登记（`Path.is_file()` 跟随链接），
    而 tar 存的是 symlink 条目 —— 目标机 `sha256sum -c` 会报可疑告警。
  · build-info.json 在 MANIFEST 之后生成，自己进不了清单，
    体检报"1 个文件没登记"。两处都是 stage_pack 的顺序/语义 bug。

检查项：
  1. 关键条目齐全
  2. MANIFEST 与实际内容**逐字节**一致；且只登记普通文件（不许有符号链接）
  3. 包内符号链接：相对路径、不悬空、目标是 MANIFEST 里的文件
  4. 轮子审计：musl / 非 aarch64 / glibc 标签 > 2.28 一律不许有
  5. 刻意排除的重物（onnxruntime / magika / …）不得出现
  6. mcp-servers.json：node 段的 entry 文件真在包里；python 段字段齐全
  7. build-info.json：lean=False、轮子数与实际一致、MCP 两侧非空、符号链接数对得上
  8. install-extras.sh 离线纯度：每个 pip 调用都带 --no-index，且不含 npx/uvx/npm install
  9. .sh 的执行位是 0755（目标机解压即用）
 10. MANIFEST 自身不出现在 MANIFEST 里（自哈希不可能）

用法：
    python3 build/verify_extras_tarball.py <tarball> [--pkg hermes-extras-offline-arm64]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tarfile
from pathlib import PurePosixPath

MUSTS = [
    "install-extras.sh", "verify-mcp.py", "merge-mcp-config.py", "README.md",
    "MANIFEST.sha256", "build-info.json", "mcp-servers.json",
    "requirements-extras.txt", "requirements-extras-heavy.txt",
    "requirements-mcp.txt", "requirements-extras.lock.txt",
    "requirements-mcp.lock.txt", "skills-official.txt",
    "mcp-node/package.json", "mcp-node/node_modules",
]

# 刻意排除 / 不该出现的重物
BANNED_SUBSTR = ("onnxruntime", "magika")

# 这些命令**永远**不该出现在离线安装脚本里（目标机没有网，只会挂到超时）
NEVER_CMD = ("npx ", "uvx ", "npm install", "npm ci", "pip download")

FAIL: list[str] = []
OK: list[str] = []


def check(cond: bool, ok: str, bad: str) -> bool:
    (OK if cond else FAIL).append(ok if cond else bad)
    print(("  ✓ " if cond else "  ✗ ") + (ok if cond else bad), flush=True)
    return cond


def logical_lines(text: str) -> list[str]:
    """把 bash 脚本折成"逻辑行"：去掉整行注释、把 `\\` 续行接起来。

    pip 调用在 install-extras.sh 里都是多行续行的，按物理行扫会漏掉
    `--no-index`（它常在续行上）。
    """
    text = re.sub(r"\\\n", " ", text)
    out = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        out.append(s)
    return out


def pip_calls_without_offline(text: str) -> list[str]:
    """找"会去连网"的 pip 调用。

    只认真正的 pip 调用行（含 `-m pip`，或行首就是 pip/pip3）。
    这样 `log_ok "... 离线 pip install 可直接取"` 这种**提示文本**不会被误判
    （第一版体检就是这么误报的：把提示语里的 "pip install" 当成联网动作）。
    """
    bad = []
    for s in logical_lines(text):
        if "-m pip" not in s and not re.match(r"^(pip3?|\$\{?\w+\}?)\s", s):
            continue
        if not re.search(r"\b(install|wheel|download)\b", s):
            continue
        if "--no-index" in s:
            continue
        bad.append(s[:120])
    return bad


def never_commands(text: str) -> list[str]:
    hits = []
    for s in logical_lines(text):
        for cmd in NEVER_CMD:
            if cmd in s:
                hits.append(f"{cmd.strip()} ← {s[:100]}")
    return hits


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("tarball")
    ap.add_argument("--pkg", default="hermes-extras-offline-arm64")
    a = ap.parse_args()

    pkg = a.pkg
    names: set[str] = set()
    modes: dict[str, int] = {}
    sizes: dict[str, int] = {}
    hashes: dict[str, str] = {}          # 只含普通文件
    links: dict[str, str] = {}           # 包内相对路径 -> linkname
    wheels: list[str] = []
    blobs: dict[str, bytes] = {}
    WANT_BLOB = {"mcp-servers.json", "build-info.json", "MANIFEST.sha256",
                 "install-extras.sh", "skills-official.txt", "package.json"}

    print(f"── 流式读取 {a.tarball} ──")
    with tarfile.open(a.tarball) as tf:
        for m in tf:
            p = PurePosixPath(m.name)
            rel = "/".join(p.parts[1:]) if p.parts and p.parts[0] == pkg else m.name
            if not rel:
                continue
            names.add(rel)
            if m.issym() or m.islnk():
                links[rel] = m.linkname
                continue
            if not m.isfile():
                continue
            modes[rel] = m.mode
            sizes[rel] = m.size
            f = tf.extractfile(m)
            h = hashlib.sha256()
            while True:
                b = f.read(1 << 20)
                if not b:
                    break
                h.update(b)
            hashes[rel] = h.hexdigest()
            base = p.name
            if base.endswith(".whl"):
                wheels.append(base)
            if base in WANT_BLOB and m.size < 1 << 20:
                f.seek(0)
                blobs[rel] = f.read()
            del f
    print(f"  普通文件 {len(hashes)} 个，符号链接 {len(links)} 个，"
          f"合计 {sum(sizes.values()) / 1048576:.1f} MiB")

    # ── 1. 关键条目 ──
    print("\n· 关键条目")
    for must in MUSTS:
        present = must in names or any(n.startswith(must + "/") for n in names)
        check(present, f"有 {must}", f"缺 {must}")

    # ── 2. 执行位 ──
    print("\n· 执行位")
    sh = [n for n in modes if n.endswith(".sh")]
    check(len(sh) >= 1, f"包里 {len(sh)} 个 .sh", "包里一个 .sh 都没有")
    bad_mode = [n for n, mo in modes.items() if n.endswith(".sh") and not (mo & 0o111)]
    check(not bad_mode, f"{len(sh)} 个 .sh 都是可执行",
          f"这些 .sh 没有执行位：{bad_mode}")

    # ── 3. MANIFEST 完整性 ──
    print("\n· MANIFEST 完整性")
    man = blobs.get("MANIFEST.sha256", b"").decode("utf-8", "replace")
    entries: dict[str, str] = {}
    for line in man.splitlines():
        if "  " in line:
            h, name = line.split("  ", 1)
            entries[name.strip()] = h.strip()
    check(len(entries) > 100, f"MANIFEST 登记 {len(entries)} 个文件",
          f"MANIFEST 只有 {len(entries)} 行，可疑")
    check("MANIFEST.sha256" not in entries,
          "MANIFEST 没有登记自己（自哈希不可能）",
          "MANIFEST 把自己也登记了 —— 自哈希不可能，写法有问题")
    wrong = [n for n, h in entries.items() if hashes.get(n) != h]
    check(not wrong, "每个 sha256 都与实际内容一致",
          f"{len(wrong)} 个文件的 sha256 对不上，例如 {wrong[:3]}")
    missing = [n for n in entries if n not in hashes]
    check(not missing, "登记的文件都在包里且是普通文件",
          f"{len(missing)} 个登记项在包里找不到（或不是普通文件），例如 {missing[:3]}")
    extra = [n for n in hashes if n not in entries and n != "MANIFEST.sha256"]
    check(not extra, "包里没有 MANIFEST 之外的漏网文件",
          f"{len(extra)} 个文件没登记，例如 {extra[:3]}")
    # 这条是踩过的坑：符号链接曾被按"目标内容"登记
    listed_links = sorted(set(entries) & set(links))
    check(not listed_links,
          "MANIFEST 里没有符号链接条目（只登记普通文件）",
          f"MANIFEST 登记了符号链接：{listed_links[:3]} —— "
          "Path.is_file() 跟随了链接，目标机 sha256sum -c 会报可疑告警")

    # ── 4. 符号链接自洽 ──
    print("\n· 符号链接")
    if links:
        abs_links = [n for n, t in links.items() if t.startswith("/")]
        check(not abs_links, f"{len(links)} 个链接都是相对路径",
              f"有绝对路径链接（解到别处会悬空）：{abs_links[:3]}")
        dangling, outside = [], []
        for n, t in links.items():
            tgt = PurePosixPath(n).parent / t
            parts: list[str] = []
            for part in tgt.parts:
                if part == "..":
                    if parts:
                        parts.pop()
                    else:
                        parts.append("..")     # 冒到包外了
                elif part != ".":
                    parts.append(part)
            resolved = "/".join(parts)
            if resolved.startswith("..") or resolved not in hashes:
                outside.append(f"{n} -> {t}")
            elif resolved not in entries:
                dangling.append(f"{n} -> {resolved}（目标未入册）")
        check(not outside, "所有链接都指向包内",
              f"这些链接悬空或跑到包外：{outside[:3]}")
        check(not dangling, "所有链接的目标都在 MANIFEST 里（不损失覆盖）",
              f"这些链接的目标未入册：{dangling[:3]}")
        over2 = [n for n in links if n.count("..") > 6]
        check(not over2, "链接层级正常", f"这些链接绕得太深：{over2[:3]}")
    else:
        print("  · 包里没有符号链接（跳过）")

    # ── 5. 轮子审计 ──
    print("\n· 轮子审计")
    check(len(wheels) > 200, f"{len(wheels)} 个轮子", f"只有 {len(wheels)} 个轮子")
    musl = [w for w in wheels if "musllinux" in w]
    check(not musl, "没有 musl 轮子", f"有 musl 轮子（glibc 机装不了）：{musl[:3]}")
    notarch = [w for w in wheels if not w.endswith(("_aarch64.whl", "-none-any.whl"))]
    check(not notarch, "所有轮子都是 aarch64 或 py3-none-any",
          f"这些轮子平台不对：{notarch[:3]}")
    over = [w for w in wheels
            if (mm := re.search(r"manylinux_2_(\d+)_aarch64", w))
            and int(mm.group(1)) > 28]
    check(not over, "所有 manylinux 标签 ≤ 2.28（信创机 glibc 基线）",
          f"这些轮子要求更新的 glibc：{over[:3]}")
    pure = sum(1 for w in wheels if w.endswith("-none-any.whl"))
    print(f"  · 纯 Python 轮子 {pure} 个，带二进制的 {len(wheels) - pure} 个")

    # ── 6. 刻意排除的重物 ──
    print("\n· 刻意排除的重物")
    hit = sorted({n for n in names for b in BANNED_SUBSTR if b in n.lower()})
    check(not hit, "没有 onnxruntime / magika 之类的重物",
          f"出现了本不该收的包：{hit[:5]}")

    # ── 7. MCP 清单 ──
    print("\n· MCP 清单")
    mj = blobs.get("mcp-servers.json", b"")
    py: list[dict] = []
    nodes: list[dict] = []
    if check(bool(mj), "读到 mcp-servers.json", "读不到 mcp-servers.json"):
        d = json.loads(mj.decode("utf-8"))
        py, nodes = d.get("python", []), d.get("node", [])
        check(len(py) >= 3, f"python 侧 {len(py)} 个服务器", f"python 侧只有 {len(py)} 个")
        check(len(nodes) >= 2, f"node 侧 {len(nodes)} 个服务器", f"node 侧只有 {len(nodes)} 个")
        fields = ("name", "package", "console", "label", "timeout")
        lack = [f"{e.get('name')} 缺 {[k for k in fields if k not in e]}"
                for e in py if not all(k in e for k in fields)]
        check(not lack, "python 侧字段齐全", f"python 侧字段不全：{lack}")
        # 离线机上没有 npx/uvx，python 服务器只能靠 venv 里的 console 脚本
        check(not [e for e in py if e.get("kind") == "node"],
              "python 侧没有混入 node 服务器", "python 侧混入了 node 服务器")
        miss = []
        for e in nodes:
            if "entry" not in e:
                miss.append(f"{e.get('name')}: 没有 entry")
            elif f"mcp-node/{e['entry']}" not in hashes:
                miss.append(f"{e.get('name')}: entry 不存在 → mcp-node/{e['entry']}")
        check(not miss, f"node 侧 {len(nodes)} 个入口文件都在包里",
              f"node 入口有问题：{miss}")
        # Node 入口必须是普通文件：运行时是 `node <entry>`，走不到 .bin 垫片
        weird = [e["entry"] for e in nodes
                 if e.get("entry", "").startswith(".bin/")]
        check(not weird, "node 入口不在 .bin/ 里（那是链接，不是真文件）",
              f"有 node 入口指向 .bin 垫片：{weird}")

    # ── 8. build-info ──
    print("\n· build-info")
    bi = blobs.get("build-info.json", b"")
    if check(bool(bi), "读到 build-info.json", "读不到 build-info.json"):
        d = json.loads(bi.decode("utf-8"))
        check(d.get("lean") is False, "lean=False（heavy 组已包含）",
              f"lean={d.get('lean')} —— 这包是不完整的 lean 版")
        n_ex = len([w for w in wheels if not w.startswith("mcp")])
        check(d.get("extras_wheels") and d.get("mcp_wheels"),
              f"构建信息里 extras={d.get('extras_wheels')} mcp={d.get('mcp_wheels')}",
              "构建信息里轮子数为 0")
        check((d.get("extras_wheels") or 0) + (d.get("mcp_wheels") or 0) == len(wheels),
              f"轮子数对得上（{d.get('extras_wheels')} + {d.get('mcp_wheels')} "
              f"= {len(wheels)}）",
              f"轮子数对不上：build-info {d.get('extras_wheels')}+{d.get('mcp_wheels')} "
              f"≠ 实际 {len(wheels)}")
        check(bool(d.get("mcp_python_servers")) and bool(d.get("mcp_node_servers")),
              f"MCP 两侧都非空：{len(d.get('mcp_python_servers', []))} + "
              f"{len(d.get('mcp_node_servers', []))}", "MCP 有一侧是空的")
        check(d.get("manifest_files") == len(entries),
              f"manifest_files 与 MANIFEST 行数一致（{len(entries)}）",
              f"build-info 写 {d.get('manifest_files')} 行，MANIFEST 实际 {len(entries)} 行")
        if "symlinks" in d:
            check(d.get("symlinks") == len(links),
                  f"符号链接数一致（{len(links)}）",
                  f"build-info 写 {d.get('symlinks')} 个链接，实际 {len(links)} 个")
        check(bool(d.get("key_versions")), "记录了关键版本", "没记录关键版本")
        print(f"  · 构建时间 {d.get('built_at')}，非 mcp 轮子约 {n_ex} 个（估算用）")

    # ── 9. 安装脚本的离线纯度 ──
    print("\n· 安装脚本的离线纯度")
    sh_src = blobs.get("install-extras.sh", b"").decode("utf-8", "replace")
    if check(bool(sh_src), "读到 install-extras.sh", "读不到 install-extras.sh"):
        check(not never_commands(sh_src),
              "没有 npx / uvx / npm install / pip download（离线机必挂）",
              f"发现永远不该有的命令：{never_commands(sh_src)[:3]}")
        online = pip_calls_without_offline(sh_src)
        check(not online, "每个 pip 调用都带 --no-index",
              f"这些 pip 调用会去连网：{online[:3]}")
        check("--no-index" in sh_src or "PIP_NO_INDEX" in sh_src,
              "pip 调用带 --no-index / PIP_NO_INDEX",
              "没看到 --no-index —— 目标机上 pip 会去连网")
        check("merge-mcp-config.py" in sh_src, "会合并 MCP 配置",
              "没调用 merge-mcp-config.py")
        check("verify-mcp.py" in sh_src, "会做 MCP 握手验证", "没调用 verify-mcp.py")
        n_sh = len(sh_src.splitlines())
        print(f"  · install-extras.sh {n_sh} 行")

    sk = blobs.get("skills-official.txt", b"").decode("utf-8", "replace")
    n_sk = len([l for l in sk.splitlines() if l.strip() and not l.startswith("#")])
    check(n_sk >= 10, f"skills-official.txt 有 {n_sk} 个 official skill",
          f"skills-official.txt 只有 {n_sk} 条")

    print()
    print(f"通过 {len(OK)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        print("❌ 产物体检未通过")
        for x in FAIL:
            print(f"   ✗ {x}")
        return 1
    print("✅ 产物体检通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
