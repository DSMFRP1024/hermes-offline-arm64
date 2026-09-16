#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
原生模式参数不变量测试
============================================================================

这些断言锁死的是**最容易在后续维护中被改坏、而且坏了不会报错**的东西：
pip 的调用参数。一旦有人"顺手"给 pip 加上 --platform/--python-version，
构建照样成功，只是产出的包平台标签悄悄全错，要等拷到离线机才暴露。

所以把它写成测试，接在 CI 拉镜像之前跑 —— 秒级，能省下一轮几十分钟的构建。

用法：python test_native_mode.py
"""

from __future__ import annotations

import ast
import sys
import tempfile
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import build_bundle as bb  # noqa: E402

# pip 在"原生模式"下绝不允出现的开关：它们会把平台求值改回宿主之外的语义，
# 而原生模式的全部价值就在于平台本来就是对的。
FORBIDDEN = ("--platform", "--python-version", "--abi", "--implementation")

REGISTRY: list[tuple[str, object]] = []


def test(name: str):
    def deco(fn):
        REGISTRY.append((name, fn))
        return fn
    return deco


def quiet(fn, *a, **kw):
    """屏蔽构建器的日志输出，只在测试里看断言结果。"""
    with mock.patch.object(bb, "LOG", lambda *x, **k: None):
        return fn(*a, **kw)


# ---------------------------------------------------------------------------
# 1. 源码层面
# ---------------------------------------------------------------------------

def source_flag_literals() -> list[str]:
    """挑出"看起来就是命令行开关"的字符串字面量。

    只认单 token 且以开关名开头的字面量 —— 这样模块文档里那句
    "--platform / --python-version 那套交叉 hack" 不会被误报。
    """
    tree = ast.parse((HERE / "build_bundle.py").read_text(encoding="utf-8"))
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            s = node.value.strip()
            if len(s.split()) == 1 and s.startswith(FORBIDDEN):
                found.append(f"line {node.lineno}: {s!r}")
    return found


@test("源码里不存在任何交叉平台开关字面量")
def t_no_cross_flags():
    bad = source_flag_literals()
    assert not bad, "原生模式不该出现这些开关:\n  " + "\n  ".join(bad)


# ---------------------------------------------------------------------------
# 2. 捕获 pip 的真实命令行
# ---------------------------------------------------------------------------

def capture_wheel_cmds() -> list[list[str]]:
    """空 lock → fetch_wheels 只跑一次批量下载就收敛，正好用来捕获 argv。"""
    captured: list[list[str]] = []

    def fake_run(cmd, *a, **kw):
        captured.append([str(c) for c in cmd])
        return 0

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        lock = td / "requirements.lock.txt"
        lock.write_text("# 故意为空\n", encoding="utf-8")
        cfg = bb.Cfg(repo=td, out=td, log_dir=td)
        with mock.patch.object(bb, "run", fake_run), mock.patch.object(bb, "LOG", lambda *a, **k: None):
            bb.fetch_wheels(cfg, lock, td / "wheels", td / "prebuilt")
    return captured


@test("pip 下载：强制 --only-binary=:all: 且无任何交叉参数")
def t_wheel_flags():
    cmds = capture_wheel_cmds()
    assert cmds, "没有捕获到任何 pip 调用"
    for argv in cmds:
        assert "download" in argv, f"意外的命令: {argv}"
        assert "--only-binary=:all:" in argv, f"wheel 阶段必须强制只用 wheel: {argv}"
        assert "--no-deps" in argv, f"必须 --no-deps（锁文件已是精确 pin）: {argv}"
        for bad in FORBIDDEN:
            assert bad not in argv, f"原生模式不允许 {bad}: {argv}"


@test("pip 下载：不带 --no-build-isolation（原生环境要允许装配构建后端）")
def t_no_build_isolation():
    for argv in capture_wheel_cmds():
        assert "--no-build-isolation" not in argv, (
            f"原生模式带 --no-build-isolation 会因缺 hatchling/poetry-core 直接失败: {argv}")


# ---------------------------------------------------------------------------
# 3. --native 前置自检
# ---------------------------------------------------------------------------

def run_preflight(machine="aarch64", sys_platform="linux", python_version=None) -> str:
    """跑 preflight_native，返回 SystemExit 的报错文本；没抛就返回空串。"""
    kwargs = {"repo": Path("."), "out": Path(".")}
    if python_version is not None:
        kwargs["python_version"] = python_version
    cfg = bb.Cfg(**kwargs)
    with mock.patch.object(bb.platform, "machine", return_value=machine), \
         mock.patch.object(bb.sys, "platform", sys_platform), \
         mock.patch.object(bb, "LOG", lambda *a, **k: None):
        try:
            bb.preflight_native(cfg)
            return ""
        except SystemExit as e:
            return str(e)


@test("非 aarch64 宿主被 --native 拒绝并给出修复指引")
def t_guard_arch():
    msg = run_preflight(machine="x86_64")
    assert msg, "x86_64 上 --native 竟然通过了自检"
    assert "aarch64" in msg, f"报错应指明架构要求: {msg}"
    assert "ubuntu-24.04-arm" in msg, f"报错应给出修复指引: {msg}"


@test("非 Linux 宿主被 --native 拒绝")
def t_guard_os():
    msg = run_preflight(sys_platform="win32")
    assert msg, "Windows 上 --native 竟然通过了自检"
    assert "Linux" in msg or "linux" in msg, msg


@test("解释器与 --python 不一致时被拒绝")
def t_guard_python():
    wrong = "9.9"
    msg = run_preflight(python_version=wrong)
    assert msg, f"--python {wrong} 与当前解释器不一致，竟然通过了自检"
    assert "解释器" in msg, msg


@test("宿主 glibc 高于目标基线时被拒绝（防在 runner 本体上误构建）")
def t_guard_glibc():
    cfg = bb.Cfg(repo=Path("."), out=Path("."), glibc=(2, 28))
    with mock.patch.object(bb, "glibc_version", return_value=(2, 39)), \
         mock.patch.object(bb, "LOG", lambda *a, **k: None):
        try:
            bb.check_host_glibc_not_newer(cfg)
        except SystemExit as e:
            assert "GNU" in str(e) or "glibc" in str(e) or "GLIBC" in str(e), e
            return
    raise AssertionError("宿主 glibc 2.39 > 目标 2.28 竟然通过了")


@test("宿主 glibc 等于目标基线时放行")
def t_guard_glibc_ok():
    cfg = bb.Cfg(repo=Path("."), out=Path("."), glibc=(2, 28))
    with mock.patch.object(bb, "glibc_version", return_value=(2, 28)), \
         mock.patch.object(bb, "LOG", lambda *a, **k: None):
        bb.check_host_glibc_not_newer(cfg)


# ---------------------------------------------------------------------------
# 4. npm 版本区间（package.json: engines.npm = "<11.10.0 || >=11.17.0"）
# ---------------------------------------------------------------------------

@test("npm 11.10–11.16 被拒绝")
def t_npm_bad_band():
    for v in ("11.10.0", "11.13.4", "11.16.9"):
        assert not bb.npm_ok(v), f"npm {v} 落在坏区间（忽略 min-release-age-exclude），不该通过"


@test("npm 10.x / 11.9 / 11.17 / 12.x 被接受")
def t_npm_good():
    for v in ("10.9.4", "11.9.9", "11.17.0", "12.0.1"):
        assert bb.npm_ok(v), f"npm {v} 应当被接受"


# ---------------------------------------------------------------------------
# 5. wheel 清点 / 标签审计
# ---------------------------------------------------------------------------

@test("wheel 文件名清点与 PEP 503 归一化")
def t_inventory():
    with tempfile.TemporaryDirectory() as td:
        d = Path(td) / "w"
        d.mkdir()
        (d / "cryptography-50.0.0-cp311-abi3-manylinux_2_28_aarch64.whl").write_bytes(b"")
        (d / "nemo_relay-0.8.3-cp311-abi3-manylinux_2_17_aarch64.whl").write_bytes(b"")
        got = bb.inventory(d)
        assert ("cryptography", "50.0.0") in got, got
        assert ("nemo-relay", "0.8.3") in got, f"nemo_relay 应归一化成 nemo-relay: {got}"


@test("标签审计拦下非 aarch64 的 wheel")
def t_audit_foreign():
    assert_audit_rejects("foo-1.0-cp311-cp311-win_amd64.whl", "平台标签")


@test("标签审计拦下 glibc 要求高于基线的 wheel")
def t_audit_glibc():
    assert_audit_rejects("foo-1.0-cp311-cp311-manylinux_2_34_aarch64.whl", "glibc")


@test("标签审计拦下混入的 sdist")
def t_audit_sdist():
    assert_audit_rejects("foo-1.0.tar.gz", "sdist")


def assert_audit_rejects(filename: str, expect: str) -> None:
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        w, p = td / "w", td / "p"
        w.mkdir()
        p.mkdir()
        (w / filename).write_bytes(b"")
        cfg = bb.Cfg(repo=td, out=td, glibc=(2, 28))
        try:
            quiet(bb.audit_wheels, cfg, w, p)
        except SystemExit as e:
            assert expect in str(e), f"报错里应含 {expect!r}: {e}"
            return
    raise AssertionError(f"{filename} 竟然通过了标签审计")


@test("标签审计放过合规的 aarch64/any wheel")
def t_audit_ok():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        w, p = td / "w", td / "p"
        w.mkdir()
        p.mkdir()
        for n in ("a-1.0-py3-none-any.whl",
                  "b-2.0-cp311-cp311-manylinux_2_28_aarch64.whl",
                  "c-3.0-cp311-abi3-manylinux_2_17_aarch64.whl"):
            (w / n).write_bytes(b"")
        cfg = bb.Cfg(repo=td, out=td, glibc=(2, 28))
        quiet(bb.audit_wheels, cfg, w, p)


# ---------------------------------------------------------------------------
# 6. lock 解析
# ---------------------------------------------------------------------------

@test("lock 解析：跳过注释与可编辑行、容忍空格、保留 marker")
def t_parse_lock():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "l.txt"
        p.write_text(
            "# comment\n"
            "cryptography==50.0.0\n"
            "nemo-relay==0.8.3 ; sys_platform == 'linux'\n"
            "-e .\n"
            "\n"
            "psutil == 7.2.2\n",
            encoding="utf-8")
        got = bb.parse_lock(p)
        assert {n for n, _, _ in got} == {"cryptography", "nemo-relay", "psutil"}, got
        assert all(v for _, v, _ in got), got


# ---------------------------------------------------------------------------
# 7. CLI
# ---------------------------------------------------------------------------

@test("--native 是必填参数")
def t_cli_native_required():
    import contextlib
    import io
    with contextlib.redirect_stderr(io.StringIO()):
        try:
            bb.make_argparser().parse_args([])
        except SystemExit:
            return
    raise AssertionError("--native 应当必填（本工具只支持原生模式）")


@test("默认基线 glibc 2.28 / Node 24 线 / Python 3.11")
def t_cli_defaults():
    a = bb.make_argparser().parse_args(["--native"])
    assert a.glibc == "2.28", a.glibc
    assert a.node_line == 24, a.node_line
    assert a.python_version == "3.11", a.python_version


# ---------------------------------------------------------------------------
# 8. 行尾（CRLF 的 .sh 拷到 Linux 会 bad interpreter）
# ---------------------------------------------------------------------------

@test("shell 脚本必须是 LF 行尾")
def t_lf():
    bad = []
    for sub in ("target",):
        d = HERE.parent / sub
        if not d.exists():
            continue
        for f in sorted(d.rglob("*.sh")):
            if b"\r\n" in f.read_bytes():
                bad.append(str(f.relative_to(HERE.parent)))
    assert not bad, "这些脚本是 CRLF 行尾，Linux 上会报 bad interpreter: " + ", ".join(bad)


# ---------------------------------------------------------------------------

def main() -> int:
    print("=" * 64)
    print("  原生模式参数不变量测试")
    print("=" * 64)
    passed = failed = 0
    for name, fn in REGISTRY:
        try:
            fn()
        except AssertionError as e:
            failed += 1
            print(f"✗ {name}\n    {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"✗ {name}\n    {type(e).__name__}: {e}")
        else:
            passed += 1
            print(f"✓ {name}")
    print("-" * 64)
    print(f"通过 {passed} / 共 {passed + failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
