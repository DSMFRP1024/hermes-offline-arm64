#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`build_extras.py pack` 的端到端冒烟测试（秒级，无网络）。

为什么需要它：静态门能查"引用不存在的名字"，却查不出
「某个子命令缺一个选项、而 main() 无条件读它」这类**只在运行到那一步才爆**
的问题。真实案例（CI run 35413545870）：

    pack 阶段 → args.index = args.index or None
    AttributeError: 'Namespace' object has no attribute 'index'

前面六步（静态检查、容器下轮子、npm 装 MCP）全绿，只有最后一步炸，
日志里还看不出与 `--index` 有关 —— 白白烧掉一轮十几分钟的 CI。

这里用一棵最小合成树把 pack 真跑一遍：只要它走到
「生成 MANIFEST.sha256 → pack 阶段完成」并产出 tarball，就算通过。

用法：
    python build/smoke_pack.py
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILDER = ROOT / "build" / "build_extras.py"


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
    # node 留空 → 跳过 Node 侧解析，不需要 node_modules，测试才能秒级跑完
    (src / "mcp-servers.json").write_text(json.dumps({
        "python": [{"name": "smoke", "package": "smoke-mcp",
                    "console": "smoke-mcp", "label": "smoke", "timeout": 30}],
        "node": [],
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    if not BUILDER.is_file():
        print(f"✗ 找不到 {BUILDER}")
        return 1

    with tempfile.TemporaryDirectory(prefix="smoke-pack-") as td:
        tmp = Path(td)
        src = tmp / "hermes-extras"
        out = tmp / "pkg"
        tb = tmp / "hermes-extras-offline-arm64.tar.gz"
        build_fixture(src)

        cmd = [sys.executable, "-u", str(BUILDER), "pack",
               "--src", str(src), "--out", str(out), "--tarball", str(tb)]
        print("$ " + " ".join(cmd))
        p = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=300)
        blob = (p.stdout or "") + (p.stderr or "")
        print(p.stdout, end="")
        if p.stderr and p.stderr.strip():
            print("── stderr ──")
            print(p.stderr[-2000:])

        problems: list[str] = []
        if "AttributeError" in blob:
            problems.append("出现 AttributeError —— 多半是 main() 读了某个子命令"
                            "没定义的选项")
        if p.returncode != 0:
            problems.append(f"退出码 {p.returncode}")
        for must in ("生成 MANIFEST.sha256", "pack 阶段完成"):
            if must not in blob:
                problems.append(f"没有走到「{must}」")
        if not tb.is_file():
            problems.append("没有产出 tarball")

        if problems:
            print()
            for x in problems:
                print(f"✗ {x}")
            print("❌ pack 端到端冒烟失败")
            return 1
        print(f"\n  tarball {tb.stat().st_size} B")

    print("✅ pack 端到端冒烟通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
