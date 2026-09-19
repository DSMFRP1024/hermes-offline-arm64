#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""工作流自检：YAML 结构 / run 块 shell 语法 / inputs 引用完整性 / pipefail 陷阱。

为什么值得作为一个 CI 前置门：

    workflow 出错的地方几乎从来不是 YAML 语法 —— 那会当场报错。真正难查的是：

    1. `run:` 块里的 shell 引号 / heredoc / 管道写法错，
       要等一整轮构建跑到那一步才炸；
    2. 引用了 `github.event.inputs.foo`，但 `workflow_dispatch.inputs` 里
       根本没声明 `foo`。YAML 合法、shell 合法、CI 一路绿灯，
       唯独那行取到的是**空串** —— 行为静默偏离预期，最难发现；
    3. `set -o pipefail` 下的提前退出消费者。`cmd | head -40` 里 head 读够
       就退出并关闭管道，cmd 立刻吃到 EPIPE（`tar: stdout: write error`，
       退出码 2），被 pipefail 放大成"整条流水线失败"。于是产物明明完好，
       报错却落在校验步骤上，非常容易被误判成构建失败 —— 这个坑
       真实发生过一次，所以这里把它固化成一条 lint。

本文件只读工作流，不修改它。

用法：python build/test_workflow.py
需要一个可用的 bash（Windows 上取 Git for Windows 的 bash）。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WFS = [
    ROOT / ".github" / "workflows" / "build-offline-bundle.yml",
    ROOT / ".github" / "workflows" / "build-extras.yml",
]
SHELLS = [ROOT / "build" / "ci-entry.sh",
          ROOT / "build" / "ci-extras-entry.sh",
          ROOT / "target" / "install.sh",
          ROOT / "target" / "check-env.sh",
          ROOT / "extras" / "install-extras.sh"]

FAIL: list[str] = []


def check(cond: bool, ok: str, bad: str) -> bool:
    print(("  ✓ " if cond else "  ✗ ") + (ok if cond else bad))
    if not cond:
        FAIL.append(bad)
    return cond


def find_bash() -> str | None:
    cands = [
        os.environ.get("BASH"),
        shutil.which("bash"),
        r"D:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files\Git\bin\bash.exe",
        "/bin/bash",
        "/usr/bin/bash",
    ]
    for c in cands:
        if c and Path(c).exists():
            return c
    return None


def bash_syntax(bash: str, script: str, where: str) -> bool:
    """用 `bash -n -s` 从 stdin 读脚本做语法检查（不落临时文件）。"""
    p = subprocess.run([bash, "-n", "-s"], input=script,
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    if p.returncode == 0:
        return True
    print(f"  ✗ {where} shell 语法错误：")
    for line in (p.stderr or "").strip().splitlines()[:8]:
        print(f"      {line}")
    FAIL.append(f"{where} shell 语法错误")
    return False


# ── pipefail 陷阱 lint ──────────────────────────────────────────────────

def lint_pipefail(script: str, where: str) -> bool:
    """找出 `... | head` / `... | grep -q` 这类"最后一段提前退出"的管道。

    在 `set -o pipefail` 的脚本里，最后一段（head / grep -q）读够就退出并关闭
    管道，上游命令随即吃 EPIPE 退出非零，pipefail 把整条管道判为失败。危害有两档：

      · 普通语句 → 整条脚本中止（`tar: stdout: write error` 那种）；
      · `if ! ... | grep -q` → 状态码被 `!` 反转，条件恒为真，
        "已在 PATH 里" 也会被判成 "不在 PATH 里"，逻辑静默反向。

    只对**最后一段**是真命令的情况报警。两处例外：
      - `head -40 f | sed ...`：head 在最前，没有上游写入者被它害到；
      - `... | head -1 || true`：`||` 已经把退出码吃掉，失败被显式处理，
        脚本不会中止（虽不优雅，但不属于这个 bug）。
    """
    esc = "\x00"  # 占位符：先把逻辑或 `||` 换成它，免得被当成管道切错
    hard, inverted = [], []
    for ln, line in enumerate(script.splitlines(), 1):
        code = line.split("#", 1)[0]
        if "|" not in code:
            continue
        parts = [p.strip() for p in code.replace("||", esc).split("|")]
        parts = [p for p in parts if p]
        if len(parts) < 2:
            continue
        if esc in parts[-1]:
            continue  # `|| ...` 兜住了退出码
        last = re.split(r"\s*(?:>>|>)\s*", parts[-1])[0].strip()  # 去掉重定向
        toks = last.split()
        if not toks:
            continue
        head_cmd, flags = toks[0], [t for t in toks[1:] if t.startswith("-")]
        if not (head_cmd == "head" or (
                head_cmd in ("grep", "rg")
                and any(("q" in f or "m" in f) for f in flags))):
            continue
        if re.search(r"(?:^|\s)!\s", parts[0]):
            inverted.append((ln, line.strip()))
        else:
            hard.append((ln, line.strip()))

    ok = True
    if hard:
        print(f"  ✗ {where} 管道末段提前退出，pipefail 下会中止脚本：")
        for ln, line in hard:
            print(f"      行 {ln}: {line}")
        FAIL.append(f"{where} pipefail 提前退出管道")
        ok = False
    if inverted:
        print(f"  ✗ {where} 取反条件里的提前退出管道 —— 逻辑会被静默反转：")
        for ln, line in inverted:
            print(f"      行 {ln}: {line}")
        FAIL.append(f"{where} 取反条件逻辑反转")
        ok = False
    return ok


# ── 主流程 ──────────────────────────────────────────────────────────────

def check_one_workflow(wf: Path, yaml) -> None:
    """对单个工作流跑全套结构检查。"""
    print(f"\n· {wf.relative_to(ROOT).as_posix()}")
    if not wf.is_file():
        check(False, "", f"{wf.relative_to(ROOT).as_posix()} 不存在")
        return
    text = wf.read_text(encoding="utf-8")
    # PyYAML 按 YAML 1.1 解析，裸键 `on` 会被当成布尔 True。所以两种键都要试，
    # 否则 `on: workflow_dispatch:` 会被静默读成"没有触发器"。
    doc = yaml.safe_load(text)
    trig = doc.get("on") or doc.get(True)
    check(trig is not None, "YAML 可解析，且存在触发器", "解析不出触发器（YAML 结构错）")
    if trig is None:
        return

    jobs = doc.get("jobs") or {}
    check(bool(jobs), f"jobs 已声明（{', '.join(jobs)}）", "没有 jobs")

    # 单 job 的工作流取那个 job；多 job 的一律要求有 build。
    build = jobs.get("build") or next(iter(jobs.values()), {})
    runs_on = str(build.get("runs-on", ""))
    check("arm" in runs_on.lower(),
          f"runs-on = {runs_on}（含 arm，架构对）",
          f"runs-on = {runs_on!r} 看起来不是 arm64 runner")
    check(bool(build.get("timeout-minutes")),
          f"timeout-minutes = {build.get('timeout-minutes')}",
          "没有 timeout-minutes：跑飞了会占满默认 6 小时额度")
    check((doc.get("permissions") or {}).get("contents") == "write",
          "permissions.contents = write（Release 步骤需要）",
          "permissions.contents 不是 write")

    steps = build.get("steps") or []
    joined = "\n".join(str(s.get("run", "")) for s in steps)
    check("manylinux_2_28_aarch64" in joined,
          "构建步骤跑在 manylinux_2_28_aarch64 容器里（glibc 基线正确）",
          "构建步骤没找到 manylinux_2_28_aarch64 容器 —— "
          "只用 arm64 runner 会产出 glibc 2.39 的包")

    # inputs 引用完整性
    declared = set(((trig.get("workflow_dispatch") or {}).get("inputs") or {}).keys())
    used = set(re.findall(r"github\.event\.inputs\.([A-Za-z0-9_-]+)", text))
    unknown = sorted(used - declared)
    check(not unknown,
          f"引用的 inputs 全部已声明（{len(used)} 个引用 / {len(declared)} 个声明）",
          f"引用了未声明的 input：{unknown}（会静默取到空串）")

    # 每个 run 块都过一遍 bash -n + pipefail lint
    bash = find_bash()
    if not bash:
        print("  ! 找不到 bash，跳过 shell 语法检查")
    n_run = 0
    for i, s in enumerate(steps):
        script = s.get("run")
        if not isinstance(script, str) or not script.strip():
            continue
        n_run += 1
        name = s.get("name") or f"step[{i}]"
        if bash:
            bash_syntax(bash, script, f"步骤「{name}」")
        if re.search(r"set\s+-[a-z]*o\s+pipefail|set\s+-euo\s+pipefail", script):
            lint_pipefail(script, f"步骤「{name}」")
    check(n_run > 0, f"{n_run} 个 run 块 shell 语法与 pipefail 陷阱检查完毕",
          "workflow 里一个 run 块都没有，检查等于没跑")


# ── 主流程 ──────────────────────────────────────────────────────────────

def main() -> int:
    print("── 工作流自检 ──")

    try:
        import yaml
    except ImportError:
        # 不做硬依赖：本机可能没装，先自己补一次再放弃。
        print("  · 缺少 PyYAML，尝试自动安装...")
        subprocess.run([sys.executable, "-m", "pip", "install", "--quiet",
                        "--disable-pip-version-check", "pyyaml"],
                       capture_output=True)
        try:
            import yaml  # noqa: F811
        except ImportError:
            print("  ✗ 仍缺 PyYAML。手动装：pip install pyyaml")
            return 2

    any_wf = False
    for wf in WFS:
        if wf.is_file():
            any_wf = True
            check_one_workflow(wf, yaml)
    check(any_wf, f"共 {sum(1 for w in WFS if w.is_file())} 个工作流文件已检查",
          "一个工作流文件都没找到")

    # 仓库里的 shell 脚本也一起查
    print("\n· 仓库内 shell 脚本")
    bash = find_bash()
    for sh in SHELLS:
        if not sh.exists():
            check(False, "", f"{sh.relative_to(ROOT).as_posix()} 不存在")
            continue
        if bash:
            p = subprocess.run([bash, "-n", str(sh)], capture_output=True,
                               text=True, encoding="utf-8", errors="replace")
            check(p.returncode == 0, f"{sh.relative_to(ROOT).as_posix()} 语法 OK",
                  f"{sh.relative_to(ROOT).as_posix()} 语法错误：{(p.stderr or '').strip()[:200]}")
        lint_pipefail(sh.read_text(encoding="utf-8"), sh.relative_to(ROOT).as_posix())

    print()
    if FAIL:
        print(f"❌ 工作流自检失败（{len(FAIL)} 项）")
        return 1
    print("✅ 工作流自检通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
