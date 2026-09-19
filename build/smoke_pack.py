#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`build_extras.py pack` 的端到端冒烟测试（秒级，无网络）。

为什么需要它：静态门能查"引用不存在的名字"，却查不出**只在运行到那一步才爆**
的问题。两个真实案例都让 CI 白烧一整轮（前六步全绿，只有最后一步炸）：

1. CI run 35413545870 —— `pack` 没有 `--index`，`main()` 却无条件读它：
       AttributeError: 'Namespace' object has no attribute 'index'
2. CI run 35415356313 —— `resolve_node_entry()` 返回的是 `.resolve()` 过的
   **绝对**路径，而 CI 传的 `--out` 是**相对**的，`relative_to` 直接炸：
       ValueError: '.../dist/hermes-extras-offline-arm64/mcp-node/node_modules/
                     @modelcontextprotocol/server-filesystem/dist/index.js'
                   is not in the subpath of
                   'dist/hermes-extras-offline-arm64/mcp-node'

第 2 个当初漏过，是因为冒烟测试**传的是绝对路径、且没传 --node-src** ——
那条出错的代码路径一次都没被执行。

所以本冒烟刻意做两件事，把"测试和 CI 不同形"这个坑本身堵死：
  · **两种传参各跑一遍**：全相对（＝CI 同形）与全绝对，都必须通过；
  · 造一棵真的 `node_modules`（含 `bin` 指向嵌套 `dist/index.js` 的包），
    强制走 `resolve_node_entry` + `relative_to` 那条路径。

用法：
    python build/smoke_pack.py
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "build"
BUILDER = BUILD / "build_extras.py"
VERIFIER = BUILD / "verify_extras_tarball.py"

NODE_PKG = "@modelcontextprotocol/server-filesystem"
PKG_DIR = "hermes-extras-offline-arm64"


def rm(path: Path) -> None:
    """删文件，且不被本机回收站 shim 的假失败带崩。

    这台机器上 os.unlink 被劫持成"送回收站"，失败时会抛 OSError ——
    而它经常**已经删成功**了照样抛。所以捕获后复核，复核不了也不致命。
    """
    try:
        if not path.exists():
            return
        try:
            path.write_bytes(b"")          # 先瘦身，绕开回收站单文件配额
        except OSError:
            pass
        path.unlink()
    except OSError:
        pass
    if path.exists():                      # 真没删掉就走 Win32 直调
        try:
            import ctypes
            ctypes.WinDLL("kernel32").DeleteFileW(str(path))
        except Exception:                  # noqa: BLE001
            pass


def fake_wheel(path: Path, dist: str, ver: str = "0.0.1") -> None:
    """造一个语法上合法的 wheel（zip + dist-info），够审计和生成锁文件用。"""
    md = (f"Metadata-Version: 2.1\nName: {dist}\nVersion: {ver}\n"
          f"Summary: smoke fixture\nRequires-Python: >=3.8\n\n")
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(f"{dist}-{ver}.dist-info/METADATA", md)
        z.writestr(f"{dist}-{ver}.dist-info/WHEEL",
                   "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
        z.writestr(f"{dist}/__init__.py", "")


def build_fixture(src: Path) -> None:
    (src / "wheels").mkdir(parents=True)
    (src / "mcp-wheels").mkdir(parents=True)
    fake_wheel(src / "wheels" / "smoke_pkg-0.0.1-py3-none-any.whl", "smoke_pkg")
    fake_wheel(src / "mcp-wheels" / "smoke_mcp-0.0.1-py3-none-any.whl", "smoke_mcp")
    (src / "requirements-extras.lock.txt").write_text(
        "smoke-pkg==0.0.1\n", encoding="utf-8")
    (src / "requirements-mcp.lock.txt").write_text(
        "smoke-mcp==0.0.1\n", encoding="utf-8")
    (src / "mcp-servers.json").write_text(json.dumps({
        "python": [{"name": "smoke", "package": "smoke-mcp",
                    "console": "smoke-mcp", "label": "smoke", "timeout": 30}],
        "node": [{"name": "filesystem", "package": NODE_PKG,
                  "arg_mode": "fs_roots", "label": "文件系统"}],
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def build_node_fixture(node_src: Path) -> bool:
    """造一棵最小的 npm 产物树。返回**是否造出了 .bin 符号链接**。

    两个关键点，各对应一个真实 bug：
      · `bin` 指向**嵌套**的 `dist/index.js` —— 只有入口比包根更深一层，
        才会真正触发 `path.relative_to(node_dest)`（CI run 35415356313 的回归点）。
      · `node_modules/.bin/` 里要有**符号链接**（npm 真实产物就是这样）——
        `Path.is_file()` 会跟随链接，若生成 MANIFEST 时不排除符号链接，
        就会按"目标内容"给它算哈希，而 tar 存的是 symlink 条目，
        校验方读不到内容 → 报"sha256 对不上 + 文件缺失"。

    ⚠️ Windows 上非管理员/未开开发者模式时 `os.symlink` 会 WinError 1314，
    所以这里允许失败 —— 失败时**降级**：本机不跑符号链接那几条断言，
    交给 CI（Linux runner 一定能建链接）覆盖。别把降级当通过。
    """
    pkg_dir = node_src / "node_modules" / Path(*NODE_PKG.split("/"))
    (pkg_dir / "dist").mkdir(parents=True)
    (pkg_dir / "package.json").write_text(json.dumps({
        "name": NODE_PKG,
        "version": "0.6.2",
        "bin": {"mcp-server-filesystem": "dist/index.js"},
    }, indent=2) + "\n", encoding="utf-8")
    (pkg_dir / "dist" / "index.js").write_text("#!/usr/bin/env node\n", encoding="utf-8")

    bin_dir = node_src / "node_modules" / ".bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    made_link = False
    try:
        os.symlink(f"../{NODE_PKG}/dist/index.js", bin_dir / "mcp-server-filesystem")
        made_link = True
    except OSError:
        # 造不出链接就放一个**普通文件**占位：至少保证目录结构在，
        # 但绝不能拿它冒充"符号链接已覆盖"
        (bin_dir / "mcp-server-filesystem").write_text("#!/bin/sh\n", encoding="utf-8")

    (node_src / "package.json").write_text(json.dumps({
        "name": "hermes-extras-mcp-node", "private": True,
        "dependencies": {NODE_PKG: "latest"},
    }, indent=2) + "\n", encoding="utf-8")
    return made_link


def run_case(label: str, tmp: Path, relative: bool, has_link: bool) -> list[str]:
    """在 tmp 里跑一遍 pack。relative=True 时命令行与 CI 完全同形。"""
    problems: list[str] = []

    def p(rel: str) -> str:
        return rel if relative else str(tmp / rel)

    cmd = [sys.executable, "-u", str(BUILDER), "pack",
           "--src", p("dist/hermes-extras"),
           "--node-src", p("dist/hermes-extras-mcp-node"),
           "--out", p("dist/pkg"),
           "--tarball", p("dist/hermes-extras-offline-arm64.tar.gz")]
    shown = [c if c != str(BUILDER) else "build/build_extras.py" for c in cmd]
    print(f"\n── {label}（relative={relative}）──")
    print("$ " + " ".join(shown))
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    r = subprocess.run(cmd, cwd=str(tmp), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=300, env=env)
    out = r.stdout or ""
    blob = out + (r.stderr or "")
    for line in out.splitlines():
        if any(k in line for k in ("✓", "✗", "!", "──")):
            print("  " + line.strip())

    if "AttributeError" in blob:
        problems.append(f"[{label}] 出现 AttributeError —— 多半是 main() "
                        "读了某个子命令没定义的选项")
    if "ValueError" in blob and "is not in the subpath of" in blob:
        problems.append(f"[{label}] relative_to 抛 ValueError —— 绝对/相对路径混用")
    if r.returncode != 0:
        problems.append(f"[{label}] 退出码 {r.returncode}")
        problems.append(f"[{label}] stderr: {(r.stderr or '')[-500:]}")

    for must in ("生成 MANIFEST.sha256", "pack 阶段完成"):
        if must not in blob:
            problems.append(f"[{label}] 没有走到「{must}」")

    # ── 产物结构 ──
    pkg = tmp / "dist" / "pkg"
    tb = tmp / "dist" / "hermes-extras-offline-arm64.tar.gz"
    if not tb.is_file():
        problems.append(f"[{label}] 没有产出 tarball")
    for must in ("MANIFEST.sha256", "build-info.json", "mcp-servers.json",
                 "install-extras.sh", "wheels", "mcp-wheels"):
        if not (pkg / must).exists():
            problems.append(f"[{label}] 产物里缺 {must}")

    man = pkg / "mcp-servers.json"
    if man.is_file():
        d = json.loads(man.read_text(encoding="utf-8"))
        nodes = d.get("node", [])
        if not nodes:
            problems.append(f"[{label}] mcp-servers.json 的 node 段是空的 —— "
                            "Node 分支没跑到")
        else:
            entry = nodes[0].get("entry", "")
            want = f"node_modules/{NODE_PKG}/dist/index.js"
            if entry != want:
                problems.append(f"[{label}] node 入口相对路径不对：{entry!r} ≠ {want!r}")
            if not (pkg / "mcp-node" / entry).is_file():
                problems.append(f"[{label}] 清单里的入口文件不存在：mcp-node/{entry}")
            if nodes[0].get("version") != "0.6.2":
                problems.append(f"[{label}] node 版本没从 package.json 取到："
                                f"{nodes[0].get('version')!r}")

    info = pkg / "build-info.json"
    if info.is_file():
        d = json.loads(info.read_text(encoding="utf-8"))
        if not d.get("mcp_node_servers"):
            problems.append(f"[{label}] build-info.json 里 mcp_node_servers 为空")
    mf = pkg / "MANIFEST.sha256"
    if mf.is_file():
        lines = mf.read_text(encoding="utf-8").splitlines()
        if len(lines) < 8:
            problems.append(f"[{label}] MANIFEST 只有 {len(lines)} 行，太少了")
        if any("\\" in ln for ln in lines):
            problems.append(f"[{label}] MANIFEST 里出现反斜杠 —— 路径没转 posix")
        if "MANIFEST.sha256" in [ln.split("  ", 1)[-1] for ln in lines]:
            problems.append(f"[{label}] MANIFEST 把自己也登记了（自哈希不可能）")
        for must in ("build-info.json",):
            if must not in [ln.split("  ", 1)[-1] for ln in lines]:
                problems.append(f"[{label}] MANIFEST 里没有 {must} —— "
                                "它的生成顺序晚于 MANIFEST，自己进不了清单")

    # ── 产物 tarball 与 MANIFEST 的一致性（这才是体检脚本看的东西）──
    if tb.is_file():
        try:
            with tarfile.open(tb) as tf:
                reg = {m.name.split("/", 1)[1] for m in tf if m.isfile()}
                sym = {m.name.split("/", 1)[1]: m.linkname
                       for m in tf if m.issym() or m.islnk()}
            man_names = {ln.split("  ", 1)[-1] for ln in
                         mf.read_text(encoding="utf-8").splitlines()} if mf.is_file() else set()
            fake = sorted(man_names - reg)
            if fake:
                problems.append(f"[{label}] MANIFEST 登记了 tar 里不是普通文件的条目："
                                f"{fake[:3]}")
            if has_link:
                if not sym:
                    problems.append(f"[{label}] 夹具建了符号链接，产物 tar 里却没有 "
                                    "symlink 条目 —— 打包把链接解引用了")
                listed = sorted(set(man_names) & set(sym))
                if listed:
                    problems.append(f"[{label}] MANIFEST 登记了符号链接：{listed[:3]} "
                                    "—— is_file() 跟随了链接，目标机 "
                                    "sha256sum -c 会报可疑告警")
                info2 = pkg / "build-info.json"
                if info2.is_file():
                    d2 = json.loads(info2.read_text(encoding="utf-8"))
                    if d2.get("symlinks") != len(sym):
                        problems.append(f"[{label}] build-info 里 symlinks="
                                        f"{d2.get('symlinks')}，实际 {len(sym)}")
        except (tarfile.TarError, OSError, ValueError) as exc:
            problems.append(f"[{label}] 读产物 tarball 失败：{exc!r}")

    # 两次都往同一个 tmp 里写，跑第二遍时先清产物，避免上一轮的残留骗过断言
    shutil.rmtree(pkg, ignore_errors=True)
    rm(tb)

    return problems


def self_test_root_cause() -> list[str]:
    """反向验证：把 `stage_pack()` 的路径归一去掉，冒烟**必须失败**。

    没有这一步，"冒烟通过"可能只是因为那条代码路径压根没被执行 ——
    这正是 bug #2 当初漏过 CI 的原因。所以这里把 bug 原样植回去：
    拿掉 src / node_src / dest 三处 `.resolve()`，按 CI 同形再跑一遍，
    要求它非零退出、且失败原因必须是 relative_to。
    """
    problems: list[str] = []
    planted = BUILDER.read_text(encoding="utf-8")
    for old, new in (
        ("src = Path(args.src).resolve()", "src = Path(args.src)"),
        ("node_src = Path(args.node_src).resolve() if args.node_src else None",
         "node_src = Path(args.node_src) if args.node_src else None"),
        ("dest = Path(args.out).resolve()", "dest = Path(args.out)"),
    ):
        if old not in planted:
            problems.append(f"反向验证失效：build_extras.py 里找不到 {old!r}"
                            "（写法改了，请同步更新这个夹具）")
        planted = planted.replace(old, new)
    if problems:
        return problems

    neg = BUILD / "_selftest_build_extras.py"
    try:
        neg.write_text(planted, encoding="utf-8")
        with tempfile.TemporaryDirectory(prefix="selftest-pack-") as td:
            tmp = Path(td).resolve()
            build_fixture(tmp / "dist" / "hermes-extras")
            build_node_fixture(tmp / "dist" / "hermes-extras-mcp-node")
            cmd = [sys.executable, "-u", str(neg), "pack",
                   "--src", "dist/hermes-extras",
                   "--node-src", "dist/hermes-extras-mcp-node",
                   "--out", "dist/pkg",
                   "--tarball", "dist/hermes-extras-offline-arm64.tar.gz"]
            env = dict(os.environ, PYTHONIOENCODING="utf-8")
            r = subprocess.run(cmd, cwd=str(tmp), capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=300,
                               env=env)
            blob = (r.stdout or "") + (r.stderr or "")
        if r.returncode == 0 or "pack 阶段完成" in blob:
            problems.append("反向验证失败：拿掉路径归一后冒烟居然还通过 —— "
                            "说明它根本没执行到 Node 那条路径")
        elif not ("not in the subpath of" in blob
                  or "入口不在 node_modules 内" in blob):
            problems.append("反向验证失败：确实失败了，但不是 relative_to 引起的"
                            "（换成了别的坑，请看一眼）")
    finally:
        rm(neg)
    return problems


def self_test_verifier() -> list[str]:
    """反向验证 `verify_extras_tarball.py` 的符号链接规则。

    为什么要**手工构造** tar 成员而不是复用上面那棵夹具树：Windows 非管理员
    建不了符号链接（WinError 1314），靠宿主建链接会让这条测试在本地静默失效。
    直接写 `TarInfo(type=SYMTYPE)` 就与操作系统无关了。

    两棵树：
      · 坏的 —— MANIFEST 把符号链接也登记了（复刻 stage_pack 的老 bug），
                体检**必须**报"MANIFEST 登记了符号链接"。
      · 好的 —— MANIFEST 只登记普通文件，体检不得报那条，
                且要确认"链接目标已入册"是通过的。
    """
    problems: list[str] = []
    if not VERIFIER.is_file():
        return [f"找不到 {VERIFIER}，符号链接规则无人把关"]

    def build(tb: Path, list_link: bool) -> None:
        target, link = "node_modules/@scope/pkg/dist/index.js", "node_modules/.bin/shim"
        body = b"#!/usr/bin/env node\n"
        with tarfile.open(tb, "w:gz") as tf:
            def adddir(rel: str) -> None:
                ti = tarfile.TarInfo(f"{PKG_DIR}/{rel}")
                ti.type = tarfile.DIRTYPE
                ti.mode = 0o755
                tf.addfile(ti)

            def addreg(rel: str, data: bytes) -> None:
                ti = tarfile.TarInfo(f"{PKG_DIR}/{rel}")
                ti.size, ti.mode = len(data), 0o644
                tf.addfile(ti, io.BytesIO(data))

            for d in ("node_modules", "node_modules/@scope",
                      "node_modules/@scope/pkg", "node_modules/@scope/pkg/dist",
                      "node_modules/.bin"):
                adddir(d)
            addreg(target, body)
            ti = tarfile.TarInfo(f"{PKG_DIR}/{link}")
            ti.type = tarfile.SYMTYPE
            ti.linkname = "../@scope/pkg/dist/index.js"
            ti.mode = 0o777
            tf.addfile(ti)

            rows = [f"{hashlib.sha256(body).hexdigest()}  {target}"]
            if list_link:
                # 复刻老 bug：Path.is_file() 跟随链接，按目标内容登记了哈希
                rows.append(f"{hashlib.sha256(body).hexdigest()}  {link}")
            man = ("\n".join(rows) + "\n").encode()
            ti = tarfile.TarInfo(f"{PKG_DIR}/MANIFEST.sha256")
            ti.size, ti.mode = len(man), 0o644
            tf.addfile(ti, io.BytesIO(man))

    def run(tb: Path) -> str:
        r = subprocess.run([sys.executable, "-u", str(VERIFIER), str(tb)],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=120,
                           env=dict(os.environ, PYTHONIOENCODING="utf-8"))
        return (r.stdout or "") + (r.stderr or "")

    with tempfile.TemporaryDirectory(prefix="selftest-verify-") as td:
        tmp = Path(td)
        bad, good = tmp / "bad.tar.gz", tmp / "good.tar.gz"
        build(bad, list_link=True)
        build(good, list_link=False)
        out_bad, out_good = run(bad), run(good)

    if "MANIFEST 登记了符号链接" not in out_bad:
        problems.append("反向验证失效：MANIFEST 登记了符号链接，体检却没报出来")
    if "MANIFEST 登记了符号链接" in out_good:
        problems.append("反向验证误报：MANIFEST 没登记符号链接，体检却说登记了")
    if "所有链接的目标都在 MANIFEST 里" not in out_good:
        problems.append("反向验证失效：好树上「链接目标已入册」那一条没通过")
    return problems


def self_test_symlink_skip() -> list[str]:
    """反向验证：把 MANIFEST 里"跳过符号链接"那句拿掉，冒烟**必须失败**。

    与 `self_test_root_cause()` 同一个道理 —— 没有植入式验证，
    "冒烟通过"可能只是那条断言压根没被触发（夹具里根本没有符号链接）。
    只在能建符号链接的主机上跑（Windows 非管理员建不了）。
    """
    problems: list[str] = []
    src = BUILDER.read_text(encoding="utf-8")
    old = "if p.is_file() and not p.is_symlink()"
    if old not in src:
        return [f"反向验证失效：build_extras.py 里找不到 {old!r}"
                "（写法改了，请同步更新这个夹具）"]
    planted = src.replace(old, "if p.is_file()")

    neg = BUILD / "_selftest_symlink_build_extras.py"
    vout = ""
    try:
        neg.write_text(planted, encoding="utf-8")
        with tempfile.TemporaryDirectory(prefix="selftest-symlink-") as td:
            tmp = Path(td).resolve()
            build_fixture(tmp / "dist" / "hermes-extras")
            build_node_fixture(tmp / "dist" / "hermes-extras-mcp-node")
            cmd = [sys.executable, "-u", str(neg), "pack",
                   "--src", "dist/hermes-extras",
                   "--node-src", "dist/hermes-extras-mcp-node",
                   "--out", "dist/pkg",
                   "--tarball", "dist/hermes-extras-offline-arm64.tar.gz"]
            subprocess.run(cmd, cwd=str(tmp), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=300,
                           env=dict(os.environ, PYTHONIOENCODING="utf-8"))
            # 在植入版产出的树上跑体检：它必须报出符号链接那条
            tb = tmp / "dist" / "hermes-extras-offline-arm64.tar.gz"
            if tb.is_file():
                rv = subprocess.run([sys.executable, "-u", str(VERIFIER), str(tb)],
                                    capture_output=True, text=True, encoding="utf-8",
                                    errors="replace", timeout=120,
                                    env=dict(os.environ, PYTHONIOENCODING="utf-8"))
                vout = (rv.stdout or "") + (rv.stderr or "")
    finally:
        rm(neg)
    if "MANIFEST 登记了符号链接" not in vout:
        problems.append("反向验证失效：拿掉「跳过符号链接」后，体检居然没报出来 —— "
                        "这条规则形同虚设")
    return problems


def main() -> int:
    if not BUILDER.is_file():
        print(f"✗ 找不到 {BUILDER}")
        return 1

    problems: list[str] = []
    with tempfile.TemporaryDirectory(prefix="smoke-pack-") as td:
        tmp = Path(td).resolve()
        # 每次都重建 fixture（run_case 会清产物，但 src/node-src 不动）
        build_fixture(tmp / "dist" / "hermes-extras")
        made_link = build_node_fixture(tmp / "dist" / "hermes-extras-mcp-node")
        if not made_link:
            print("  ! 本机建不了符号链接（WinError 1314），"
                  ".bin 那几条断言降级 —— 由 CI 的 Linux runner 覆盖")

        problems += run_case("CI 同形：全相对路径", tmp, relative=True,
                             has_link=made_link)
        problems += run_case("全绝对路径", tmp, relative=False,
                             has_link=made_link)

    print("\n── 反向验证：把路径归一拿掉，冒烟必须失败 ──")
    problems += self_test_root_cause()
    print("\n── 反向验证：体检脚本必须抓得住「MANIFEST 登记符号链接」──")
    problems += self_test_verifier()
    if made_link:
        print("\n── 反向验证：把「清单跳过符号链接」拿掉，体检必须失败 ──")
        problems += self_test_symlink_skip()
    else:
        print("\n── 跳过「拿掉符号链接跳过」的反向验证（本机建不了链接）──")
    if not problems:
        print("  ✓ 体检脚本抓得住坏树、不误判好树")

    if problems:
        print()
        for x in problems:
            print(f"✗ {x}")
        print("❌ pack 端到端冒烟失败")
        return 1
    print("\n✅ pack 端到端冒烟通过"
          "（相对/绝对两种传参、含 Node 与符号链接分支、自证有效）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
