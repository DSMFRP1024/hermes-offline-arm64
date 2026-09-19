#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线增强包静态自检。

为什么值得单独做一个前置门：

    增强包这边真正难查的失败都是**静默**的 ——

    1. requirements 里手写死版本号 → 某个版本没发 aarch64 轮子，整条构建挂，
       而且是在跑了几分钟、拉了镜像之后才挂；
    2. install-extras.sh 里出现 `npx` / `uvx` → 目标机零网络，装机全绿，
       只有真正调 MCP 工具时才连不上；
    3. 少写了 `--no-index` → 离线机上 pip 不是快速失败，而是**挂住**，
       看起来像"装到一半卡死了"；
    4. MCP 又混回 hermes 的 venv → fastmcp 把 mcp 降到 1.x，
       hermes 的原生 MCP 客户端就废了（而 `hermes doctor` 不会报错）；
    5. skills-official.txt 里写了个不存在的路径 → 装机时静默跳过，
       你以为启用了 20 个技能，实际只有 18 个。

    这些小检查都是毫秒级，却能在拉镜像**之前**把它们全部拦下来。

用法：python3 build/test_extras.py
"""

from __future__ import annotations

import ast
import json
import re
import sys
from collections.abc import Iterable
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXTRAS = ROOT / "extras"
BUILD = ROOT / "build"
WF = ROOT / ".github" / "workflows" / "build-extras.yml"
# 上游克隆；没有就跳过"路径存在性"那一项（CI 里也没有这个目录）
UPSTREAM = ROOT.parent / "src" / "hermes-agent"

FAIL: list[str] = []


def check(cond: bool, ok: str, bad: str) -> bool:
    print(("  ✓ " if cond else "  ✗ ") + (ok if cond else bad))
    if not cond:
        FAIL.append(bad)
    return cond


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def parse_reqs(path: Path) -> list[str]:
    """返回**非注释行**的原始文本（用来查钉版本）。"""
    out = []
    for raw in read(path).splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        out.append(line)
    return out


def req_names(path: Path) -> list[str]:
    return [line.split("[")[0].split("==")[0].strip() for line in parse_reqs(path)]


# ── pipefail 陷阱 lint（与 test_workflow.py 同源，但这里独立实现，
#    免得两个门互相耦合、改一个把另一个带崩）─────────────────────────────

def lint_pipefail(script: str, where: str) -> None:
    esc = "\x00"
    for ln, line in enumerate(script.splitlines(), 1):
        code = line.split("#", 1)[0]
        if "|" not in code:
            continue
        parts = [p.strip() for p in code.replace("||", esc).split("|")]
        parts = [p for p in parts if p]
        if len(parts) < 2 or esc in parts[-1]:
            continue
        last = re.split(r"\s*(?:>>|>)\s*", parts[-1])[0].strip()
        toks = last.split()
        if not toks:
            continue
        if not (toks[0] == "head" or (
                toks[0] in ("grep", "rg")
                and any(("q" in f or "m" in f) for f in toks[1:] if f.startswith("-")))):
            continue
        check(False,
              "",
              f"{where} 第 {ln} 行：管道末段提前退出，pipefail 下会中止脚本 —— {line.strip()}")


def check_lf(path: Path) -> None:
    data = path.read_bytes()
    check(b"\r\n" not in data, f"{path.name} 是 LF 行尾",
          f"{path.name} 含 CRLF —— .gitattributes 要求全部 LF")
    check(data.endswith(b"\n"), f"{path.name} 以换行结尾",
          f"{path.name} 没有以换行结尾")


# ── argparse 子命令字段一致性 ────────────────────────────────────────────
#
# 为什么需要这一项：`args.xxx` 在 main() 里被**无条件**访问、而某个子命令
# 并没有定义 `--xxx` 时，argparse 一句话都不说，直到真的跑到**那个**子命令
# 才抛 AttributeError。CI run 35413545870 就是这么挂的：静态检查、下轮子、
# npm 安装六步全绿，只有最后的 `pack` 一步炸在
#     args.index = args.index or None
# （`--index` 只挂在 `wheels` 上）。
# `check_undefined.py` 那种 AST 未定义名检查看不见「字段在不在 Namespace 上」，
# 所以必须单独钉一条。

def argparse_stages_from_src(src: str) -> tuple[dict[str, set[str]], dict[str, str]]:
    """静态解析 argparse 配置。

    返回 (stage → 该子命令可用字段集合, stage → 处理函数名)。
    """
    tree = ast.parse(src)
    parsers: dict[str, str] = {}      # 变量名 -> stage
    dests: dict[str, set[str]] = {}
    funcs: dict[str, str] = {}

    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Attribute)
                and node.value.func.attr == "add_parser"):
            var = node.targets[0].id
            stage = (node.value.args[0].value
                     if node.value.args and isinstance(node.value.args[0], ast.Constant)
                     else var)
            parsers[var] = stage
            dests.setdefault(stage, set())

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if not isinstance(node.func.value, ast.Name) or node.func.value.id not in parsers:
            continue
        stage = parsers[node.func.value.id]
        if node.func.attr == "add_argument":
            # 只取第一个字符串字面量当选项名（短名/长名只记 dest）
            for a in node.args:
                if isinstance(a, ast.Constant) and isinstance(a.value, str):
                    opt = a.value
                    dests[stage].add(opt.lstrip("-").replace("-", "_")
                                     if opt.startswith("-") else opt)
                    break
        elif node.func.attr == "set_defaults":
            for kw in node.keywords:
                if kw.arg:
                    dests[stage].add(kw.arg)
                    if kw.arg == "func" and isinstance(kw.value, ast.Name):
                        funcs[stage] = kw.value.id
    return dests, funcs


def argparse_stages(path: Path) -> tuple[dict[str, set[str]], dict[str, str]]:
    return argparse_stages_from_src(read(path))


def args_attrs(fn: ast.AST | None, name: str = "args") -> set[str]:
    """收集某函数体里所有 `args.<attr>` 的属性名。"""
    if fn is None:
        return set()
    return {n.attr for n in ast.walk(fn)
            if isinstance(n, ast.Attribute)
            and isinstance(n.value, ast.Name) and n.value.id == name}


def argparse_audit(src: str, stage_hint: Iterable[str] = ()) -> list[tuple[bool, str, str]]:
    """返回 [(ok, 通过描述, 失败描述)]，与 check() 的入参形状一致。"""
    out: list[tuple[bool, str, str]] = []
    dests, funcs = argparse_stages_from_src(src)
    stages = sorted(dests)
    if len(stages) < 2:
        out.append((False, "", f"解析出的子命令只有 {stages}（<2），检查器失效了"))
        return out
    out.append((True, f"有子命令 {stages}", ""))

    fns = {n.name: n for n in ast.walk(ast.parse(src))
           if isinstance(n, ast.FunctionDef)}

    # ① main() 里的 args.* 必须每个子命令都有 —— 否则某个子命令必崩
    common = set.intersection(*(dests[s] for s in stages))
    missing = args_attrs(fns.get("main")) - common
    out.append((not missing,
                f"main() 只访问公共字段（{len(common)} 个：{sorted(common)}）",
                f"main() 访问了不是每个子命令都有的字段：{sorted(missing)}"
                f" —— 跑到缺这个选项的子命令就 AttributeError"))

    # ② 每个阶段的处理函数只能碰自己子命令定义过的选项
    for stage in stages:
        fn = fns.get(funcs.get(stage, ""))
        if fn is None:
            continue
        bad = args_attrs(fn) - dests[stage]
        out.append((not bad,
                    f"{stage} 阶段只用它自己的选项（{len(dests[stage])} 个）",
                    f"{stage} 阶段访问了未定义选项：{sorted(bad)}"))

    for hint in stage_hint:
        out.append((hint in dests, f"有 {hint} 子命令", f"缺少 {hint} 子命令"))
    return out


# 门的反向验证夹具：一个「pack 没有 --index、main() 却读 args.index」的最小脚本。
# 这段代码**故意**是坏的，只用于 self_test_argparse_gate()，不属于构建器本身。
_FIXTURE_BUGGY = """
import argparse


def stage_wheels(args):
    args.index = args.index or None
    return args.out


def stage_pack(args):
    return args.src


def main(argv=None):
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="stage", required=True)
    w = sub.add_parser("wheels")
    w.add_argument("--out", required=True)
    w.add_argument("--index", default="")
    w.set_defaults(func=stage_wheels)
    p = sub.add_parser("pack")
    p.add_argument("--src", required=True)
    p.set_defaults(func=stage_pack)
    args = ap.parse_args(argv)
    args.index = args.index or None
    return args.func(args)
"""


def self_test_argparse_gate() -> None:
    """反向验证：这道门必须能抓住 bug，且对修好的版本不误报。

    没有反向验证的静态门等于没写 —— 正则写歪了它照样一路绿灯。
    """
    bad = [d for ok, _g, d in argparse_audit(_FIXTURE_BUGGY, ("wheels", "pack"))
           if not ok]
    check(len(bad) == 1 and "index" in bad[0],
          "反向验证：门能抓住「main() 读了子命令没有的字段」",
          f"反向验证失败 —— 带 bug 的夹具没被抓住（命中 {len(bad)} 项：{bad}）")

    fixed = _FIXTURE_BUGGY.replace("    args.index = args.index or None\n", "")
    still = [d for ok, _g, d in argparse_audit(fixed, ("wheels", "pack")) if not ok]
    check(not still,
          "反向验证：修好后门不再报（不误报）",
          f"反向验证失败 —— 修好的夹具仍被报错：{still}")


def check_argparse_fields(
        tool: Path, tool_name: str, stage_hint: Iterable[str] = ()) -> None:
    for ok, good, bad in argparse_audit(read(tool), stage_hint):
        check(ok, f"{tool_name} {good}", f"{tool_name} {bad}")


# =============================================================================

def main() -> int:
    print("── 增强包静态自检 ──")

    # ── 1. requirements ──
    print("\n· requirements（版本由构建期求值，不手写）")
    req_ex = EXTRAS / "requirements-extras.txt"
    req_hv = EXTRAS / "requirements-extras-heavy.txt"
    req_mcp = EXTRAS / "requirements-mcp.txt"
    for p in (req_ex, req_hv, req_mcp):
        check(p.is_file(), f"{p.name} 存在", f"{p.name} 不存在")
        if p.is_file():
            check_lf(p)

    ex = req_names(req_ex) if req_ex.is_file() else []
    hv = req_names(req_hv) if req_hv.is_file() else []
    mcp = req_names(req_mcp) if req_mcp.is_file() else []

    check(len(ex) >= 25, f"办公组 {len(ex)} 个顶层包", f"办公组只有 {len(ex)} 个包，太少")
    check(len(mcp) == 6, f"MCP 组 {len(mcp)} 个顶层包", f"MCP 组应是 6 个，实际 {len(mcp)}")

    # 只查**非注释行**里有没有 ==；注释里为了说明冲突会写 `mcp==1.29`，那不算钉版本。
    for name, path in (("extras", req_ex), ("heavy", req_hv), ("mcp", req_mcp)):
        if not path.is_file():
            continue
        pinned = [ln for ln in parse_reqs(path) if "==" in ln]
        check(not pinned,
              f"{name} 没有手写死版本号",
              f"{name} 里出现了 == 钉版本；版本应由构建期求值并锁进 lock：{pinned[:3]}")

    # 这几条是刻意的取舍，写进断言免得日后有人"顺手加回来"
    joined_ex = " ".join(x.lower() for x in ex + hv)
    check("markitdown" not in joined_ex,
          "没有引入 markitdown（read_file 已内置 firecrawl-anydoc）",
          "markitdown 又回来了 —— 它会连带拖进 onnxruntime/magika，功能与 "
          "read_file 的内置抽取重复")
    check("onnxruntime" not in joined_ex,
          "没有引入 onnxruntime",
          "onnxruntime 混进来了 —— 主包刻意排除的重物，确认是必须的再放行")

    # 办公刚需必须在（Pillow 主包已带，不要求出现在 extras 里）
    for need in ("python-docx", "openpyxl", "python-pptx", "pypdf",
                 "PyMuPDF", "pandas", "matplotlib"):
        check(need.lower() in joined_ex, f"办公刚需 {need} 在列表里",
              f"办公刚需 {need} 不在列表里")

    # MCP 组必须解释清为什么独立 venv（这段注释是给人看的，也是给未来的自己）
    if req_mcp.is_file():
        text = read(req_mcp)
        check("mcp == 2.0.0" in text or "mcp==2.0.0" in text,
              "requirements-mcp.txt 里说明了与 hermes 的 mcp 版本冲突",
              "requirements-mcp.txt 没写清为什么要独立 venv（这是最容易被人"
              "“顺手合并”掉的设计）")

    # ── 2. skills ──
    print("\n· optional skills 清单")
    sk = EXTRAS / "skills-official.txt"
    check(sk.is_file(), "skills-official.txt 存在", "skills-official.txt 不存在")
    rels: list[str] = []
    if sk.is_file():
        check_lf(sk)
        for raw in read(sk).splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            if not line.startswith("official/"):
                check(False, "", f"skills-official.txt 里这行不是 official/ 开头：{line}")
                continue
            rels.append(line[len("official/"):])
    check(len(rels) >= 10, f"选了 {len(rels)} 个官方可选技能",
          f"只选了 {len(rels)} 个，太少")
    check(len(rels) <= 60, f"{len(rels)} 个不超过 60（提示词索引不会爆）",
          f"选了 {len(rels)} 个 —— 技能索引会挤占上下文，精简一点")
    check(len(set(rels)) == len(rels), "没有重复项",
          f"有重复：{sorted({r for r in rels if rels.count(r) > 1})}")

    if UPSTREAM.is_dir():
        missing = [r for r in rels if not (UPSTREAM / "optional-skills" / r / "SKILL.md").is_file()]
        check(not missing,
              f"{len(rels)} 个技能在上游 optional-skills/ 里都存在",
              f"这些路径在上游不存在（装机时会被静默跳过）：{missing}")
    else:
        print("  · 没有上游克隆，跳过路径存在性检查")

    # ── 3. MCP 清单 ──
    print("\n· MCP 服务器清单")
    ms = EXTRAS / "mcp-servers.json"
    check(ms.is_file(), "mcp-servers.json 存在", "mcp-servers.json 不存在")
    man: dict = {}
    if ms.is_file():
        check_lf(ms)
        try:
            man = json.loads(read(ms))
        except json.JSONDecodeError as exc:
            check(False, "", f"mcp-servers.json 解析失败：{exc}")
            man = {}
    py_srv = man.get("python", [])
    node_srv = man.get("node", [])
    check(len(py_srv) >= 4, f"Python 侧 {len(py_srv)} 个", "Python 侧 MCP 服务器太少")
    check(len(node_srv) >= 2, f"Node 侧 {len(node_srv)} 个", "Node 侧 MCP 服务器太少")

    MODES = {"none", "fs_roots", "memory_path"}
    names = []
    for e in py_srv:
        check(all(k in e for k in ("name", "package", "console")),
              f"python:{e.get('name', '?')} 字段完整",
              f"Python 侧条目缺字段（需要 name/package/console）：{e}")
        names.append(e.get("name", ""))
    for e in node_srv:
        check(e.get("arg_mode") in MODES,
              f"node:{e.get('name', '?')} arg_mode={e.get('arg_mode')}",
              f"Node 侧 arg_mode 非法：{e.get('name')} -> {e.get('arg_mode')}")
        check(all(k in e for k in ("name", "package")),
              f"node:{e.get('name', '?')} 字段完整",
              f"Node 侧条目缺字段：{e}")
        names.append(e.get("name", ""))
    check(len(set(names)) == len(names), "服务器名唯一",
          f"服务器名有重复：{[n for n in names if names.count(n) > 1]}")
    check(all("-" not in n and "." not in n for n in names),
          "服务器名不含连字符/点（工具前缀才是 mcp_<name>_<tool> 的干净形态）",
          f"服务器名里有连字符或点，工具名前缀会变得难看：{names}")
    check(not any("fetch" in e.get("package", "") or "duckduckgo" in e.get("package", "")
                  for e in py_srv + node_srv),
          "没有收录需要外网的 MCP server",
          "收录了需要外网的 server —— 离线机上它只会失败")

    # ── 4. 构建器 ──
    print("\n· build/build_extras.py")
    bb = BUILD / "build_extras.py"
    check(bb.is_file(), "存在", "build_extras.py 不存在")
    if bb.is_file():
        check_lf(bb)
        src = read(bb)
        try:
            ast.parse(src)
            check(True, "语法 OK", "")
        except SyntaxError as exc:
            check(False, "", f"语法错误：{exc}")

        # 原生模式不变量：绝不能出现交叉下载那套**参数**。
        # 只匹配带引号的字面量 —— 文档字符串里为了解释"为什么不用它"会提到
        # `--platform`，那不该算违规。
        for banned in ("--platform", "--python-version", "--abi", "--implementation"):
            hit = re.search(rf'["\']{re.escape(banned)}["\']', src)
            check(hit is None,
                  f"没有 {banned}（原生模式：容器给基线，不用交叉 hack）",
                  f"出现了 {banned} 参数 —— 交叉模式下 PEP 508 标记按宿主求值，"
                  f"linux-only 依赖会被静默漏掉")
        check("--only-binary" in src, "--only-binary=:all: 已写死（目标机没有编译器）",
              "没有 --only-binary，sdist 会被下进来，装机时编译必挂")
        check("musllinux" in src, "审计里拒 musl 轮子",
              "审计没排除 musl 轮子（glibc 机加载不了）")
        check("manylinux_2_" in src, "审计里有 manylinux 基线解析",
              "审计里没有解析 manylinux 基线")
        # SDIST_PKGS 里每个包都得真的在需求文件里，否则是死代码
        m = re.search(r"SDIST_PKGS\s*=\s*\{([^}]*)\}", src)
        if m:
            sd = {s.strip().strip('"\'') for s in m.group(1).split(",") if s.strip()}
            check(bool(sd), "声明了 sdist 构建包", "SDIST_PKGS 是空的")
            stray = {s for s in sd if s.lower() not in {p.lower() for p in ex + hv}}
            check(not stray, f"SDIST_PKGS {sorted(sd)} 都能在需求文件里找到",
                  f"SDIST_PKGS 里有需求文件里没有的包（死代码）：{sorted(stray)}")
        # pack 阶段必须把两个辅助脚本一起带上，否则目标机上会缺文件
        for f in ("install-extras.sh", "verify-mcp.py", "merge-mcp-config.py",
                  "mcp-servers.json", "skills-official.txt"):
            check(f in src, f"pack 阶段带上 {f}", f"pack 阶段没带 {f}")
        check("0o755" in src, "打包时强制脚本执行位",
              "没有强制执行位 —— 目标机解压后 install-extras.sh 可能不可执行")

    # ── 5. 目标机安装脚本 ──
    print("\n· extras/install-extras.sh")
    ins = EXTRAS / "install-extras.sh"
    check(ins.is_file(), "存在", "install-extras.sh 不存在")
    if ins.is_file():
        check_lf(ins)
        src = read(ins)
        check(set(src.splitlines()[0:1]) and src.startswith("#!/usr/bin/env bash"),
              "shebang 正确", "shebang 不是 #!/usr/bin/env bash")
        check("set -euo pipefail" in src, "set -euo pipefail", "缺 set -euo pipefail")
        check("--no-index" in src, "pip 全程 --no-index",
              "缺 --no-index —— 离线机上 pip 会挂住而不是快速失败")
        check("PIP_NO_INDEX=1" in src, "PIP_NO_INDEX=1 兜底",
              "缺 PIP_NO_INDEX=1")
        # 零网络：不能有任何 npx / uvx
        for banned in ("npx", "uvx", "npm install", "pip download"):
            check(not re.search(rf"(^|[^a-z-]){re.escape(banned)}", src),
                  f"没有 {banned}（目标机零网络）",
                  f"脚本里出现 {banned} —— 离线机上必挂")
        check('MCP_VENV="$MCP_DIR/venv"' in src,
              "MCP 用独立 venv（不碰 hermes 的 venv）",
              "MCP venv 路径不对 —— 必须与 hermes 的 venv 分开")
        check("merge-mcp-config.py" in src, "config.yaml 走专用合并脚本",
              "没有用 merge-mcp-config.py 改配置")
        check("config.yaml" in src and ">> \"$CFG\"" not in src,
              "没有用追加的方式改 config.yaml",
              "脚本直接往 config.yaml 追加内容 —— 会破坏 YAML 结构")
        check("optional-skills" in src, "技能从本地 optional-skills 取（零下载）",
              "技能没有走本地 optional-skills")
        check("verify-mcp.py" in src, "装完做 MCP 真实握手",
              "没有调用 verify-mcp.py —— 少了唯一能证明 MCP 真能起来的环节")

        # 步骤号必须从 0 连续编号，缺号说明有一步被删掉了
        steps = [int(m.group(1)) for m in re.finditer(r'^log_step "(\d+)\. ', src, re.M)]
        check(steps == list(range(len(steps))),
              f"步骤号连续：{steps}",
              f"步骤号不连续（删步骤时漏改了）：{steps}")
        check(len(steps) >= 8, f"共 {len(steps)} 步", f"只有 {len(steps)} 步，不完整")

        lint_pipefail(src, "install-extras.sh")

    # ── 6. 辅助脚本 ──
    print("\n· 随包脚本（目标机上跑，必须只用标准库）")
    for name in ("verify-mcp.py", "merge-mcp-config.py"):
        p = EXTRAS / name
        check(p.is_file(), f"{name} 存在", f"{name} 不存在")
        if not p.is_file():
            continue
        check_lf(p)
        src = read(p)
        try:
            tree = ast.parse(src)
            check(True, f"{name} 语法 OK", "")
        except SyntaxError as exc:
            check(False, "", f"{name} 语法错误：{exc}")
            continue
        mods = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                mods |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                mods.add(node.module.split(".")[0])
        if name == "verify-mcp.py":
            third = mods - {"argparse", "json", "os", "queue", "subprocess",
                            "sys", "threading", "time", "__future__"}
            check(not third, "verify-mcp.py 只用标准库（用 hermes 的 python 就能跑）",
                  f"verify-mcp.py 引入了第三方库：{sorted(third)}")
        else:
            # 合并脚本允许 ruamel/yaml，但必须是"优先 ruamel、退回 PyYAML"
            check("ruamel" in src, "merge-mcp-config.py 优先用 ruamel 保注释",
                  "没优先用 ruamel —— config.yaml 里的注释会被抹掉")

    # ── 7. 工作流 ──
    print("\n· .github/workflows/build-extras.yml")
    check(WF.is_file(), "存在", "build-extras.yml 不存在")
    if WF.is_file():
        check_lf(WF)
        wf = read(WF)
        check("manylinux_2_28_aarch64" in wf,
              "容器是 manylinux_2_28_aarch64（glibc 2.28 基线）",
              "没有用到 manylinux 容器 —— 只用 arm64 runner 会产出 glibc 2.39 的轮子")
        check("ubuntu-24.04-arm" in wf, "跑在 arm64 runner 上", "不是 arm64 runner")
        check("build/build_extras.py pack" in wf, "调用了 pack 阶段", "没有调用 pack 阶段")
        check("build/ci-extras-entry.sh" in wf, "调用了容器入口", "没有调用容器入口脚本")
        check("build/test_extras.py" in wf, "把本自检挂进了 CI", "本自检没挂进 CI")
        check("build/smoke_pack.py" in wf,
              "把 pack 端到端冒烟挂进了 CI",
              "pack 冒烟没挂进 CI —— 「子命令缺选项」这类问题就只能到 CI 才暴露")
        check("build/verify_extras_tarball.py" in wf,
              "打完包立刻跑产物侧体检（同一份规则，别等下载回来才发现）",
              "CI 没跑 verify_extras_tarball.py —— 产物语义问题会漏到交付环节")
        check("chown" in wf,
              "容器跑完后 chown dist（否则 runner 写不进打包产物）",
              "没有 chown dist —— 容器以 root 落盘，后续 runner 步骤会 Permission denied")
        check("hermes-extras-offline-arm64.tar.gz" in wf, "上传增强包 tarball",
              "没有上传增强包")
        lint_pipefail(
            "\n".join(re.findall(r"^\s+run: \|\n((?:\s{10,}.*\n)+)", wf, re.M)),
            "workflow run 块")

    # ── 8. 构建器 CLI：子命令字段一致性 ──
    print("\n· build_extras.py 的 argparse 字段")
    self_test_argparse_gate()
    if bb.is_file():
        try:
            check_argparse_fields(bb, "build_extras.py", stage_hint=("wheels", "pack"))
        except SyntaxError as exc:
            check(False, "", f"build_extras.py 语法错误：{exc}")

    sp = BUILD / "smoke_pack.py"
    check(sp.is_file(), "build/smoke_pack.py 存在（pack 的端到端兜底）",
          "build/smoke_pack.py 不存在 —— pack 阶段就只能靠 CI 才能验到")
    if sp.is_file():
        check_lf(sp)
        try:
            ast.parse(read(sp))
            check(True, "smoke_pack.py 语法 OK", "")
        except SyntaxError as exc:
            check(False, "", f"smoke_pack.py 语法错误：{exc}")

        # 「测试必须与 CI 同形」本身也要被守住 —— bug #2 就是因为冒烟传了绝对
        # 路径、又没给 --node-src，出错的代码路径一次都没跑到。
        sps = read(sp)
        check("--node-src" in sps,
              "冒烟传了 --node-src（覆盖 Node 入口解析）",
              "冒烟没传 --node-src —— resolve_node_entry/relative_to 那条路径"
              "不会被跑到，CI run 35415356363 就是这么漏过去的")
        check("build_node_fixture" in sps,
              "冒烟造了真的 node_modules（bin 指向嵌套入口）",
              "冒烟没造 node_modules —— Node 分支形同虚设")
        check("relative=True" in sps and "relative=False" in sps,
              "冒烟对「相对 / 绝对」两种传参各跑一遍",
              "冒烟只跑一种传参 —— 绝对/相对混用这类 bug 会漏")
        check("self_test_root_cause" in sps,
              "冒烟内置反向验证（植回 bug 必须被抓）",
              "冒烟没有自证 —— 「通过」可能只是没执行到那条路径")

    # ── 8.5 MANIFEST 的语义与顺序（交付前体检踩出来的两个真 bug）──
    # ① npm 的 node_modules/.bin/* 是符号链接，而 `Path.is_file()` 会**跟随**链接，
    #    于是 MANIFEST 按"目标内容"给链接算了哈希；tar 存的是 symlink 条目，
    #    校验方按链接读不到内容 → 同时报"sha256 对不上"和"文件缺失"。
    # ② build-info.json 若排在 MANIFEST 之后生成，它自己进不了清单 → "1 个文件没登记"。
    if bb.is_file():
        src = read(bb)
        i = src.find("def _manifest_files(")
        mf_body = src[i:i + 1200] if i >= 0 else ""
        check("not p.is_symlink()" in mf_body,
              "MANIFEST 只登记普通文件（跳过符号链接）",
              "MANIFEST 生成时没跳过符号链接 —— Path.is_file() 会跟随链接，"
              "npm 的 .bin/* 会被按目标内容登记，目标机 sha256sum -c 报可疑告警")
        j = src.find("def stage_pack(")
        pack_body = src[j:] if j >= 0 else ""
        k_info = pack_body.find("生成 build-info.json")
        k_man = pack_body.find("生成 MANIFEST.sha256")
        check(0 <= k_info < k_man,
              "build-info.json 先于 MANIFEST 生成（它自己也能入册）",
              f"build-info 在 MANIFEST 之后（{k_info} vs {k_man}）—— "
              "它自己会漏在清单外，体检报「1 个文件没登记」")
        check("_write_info(dest, info)" in pack_body,
              "build-info 通过 _write_info 统一落盘",
              "没有 _write_info —— 两份写法容易漂移")
        check("symlinks" in pack_body,
              "build-info 记录了符号链接数（体检据此对账）",
              "build-info 没记符号链接数")

    # ── 8.6 产物侧体检脚本 ──
    print("\n· build/verify_extras_tarball.py")
    vf = BUILD / "verify_extras_tarball.py"
    check(vf.is_file(), "存在（产物侧的唯一裁判）",
          "不存在 —— 打包侧的语义问题就没有权威裁判，只能等下载完才发现")
    if vf.is_file():
        check_lf(vf)
        vsrc = read(vf)
        try:
            tree = ast.parse(vsrc)
            mods: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    mods |= {a.name.split(".")[0] for a in node.names}
                elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                    mods.add(node.module.split(".")[0])
            third = mods - {"argparse", "hashlib", "json", "re", "sys", "tarfile",
                            "pathlib", "__future__"}
            check(not third,
                  "只用标准库（CI 里 python3 裸跑即可，不用装任何东西）",
                  f"引入了第三方库：{sorted(third)}")
        except SyntaxError as exc:
            check(False, "", f"verify_extras_tarball.py 语法错误：{exc}")
        # 关键规则必须在，且 pip 判定不能靠裸子串（第一版就把提示语里的
        # "pip install" 误判成联网动作）
        for needle, what in (
            ("MANIFEST 登记了符号链接", "把「清单登记了符号链接」判为失败"),
            ("所有链接的目标都在 MANIFEST 里", "校验符号链接目标已入册"),
            ("每个 pip 调用都带 --no-index", "校验每个 pip 调用都离线"),
            ("if \"-m pip\" not in s", "pip 判定锚在真正的调用行上（不误判提示语）"),
            ("BANNED_SUBSTR", "排除 onnxruntime / magika 之类的重物"),
            ("NEVER_CMD", "禁止 npx / uvx / npm install / pip download"),
        ):
            check(needle in vsrc, f"体检脚本会{what}", f"体检脚本缺规则：{what}")

    sp = BUILD / "smoke_pack.py"
    check(sp.is_file(), "build/smoke_pack.py 存在（pack 的端到端兜底）",
          "build/smoke_pack.py 不存在 —— pack 阶段就只能靠 CI 才能验到")
    if sp.is_file():
        check_lf(sp)
        try:
            ast.parse(read(sp))
            check(True, "smoke_pack.py 语法 OK", "")
        except SyntaxError as exc:
            check(False, "", f"smoke_pack.py 语法错误：{exc}")

        # 「测试必须与 CI 同形」本身也要被守住 —— bug #2 就是因为冒烟传了绝对
        # 路径、又没给 --node-src，出错的代码路径一次都没跑到。
        sps = read(sp)
        check("--node-src" in sps,
              "冒烟传了 --node-src（覆盖 Node 入口解析）",
              "冒烟没传 --node-src —— resolve_node_entry/relative_to 那条路径"
              "不会被跑到，CI run 35415356363 就是这么漏过去的")
        check("build_node_fixture" in sps,
              "冒烟造了真的 node_modules（bin 指向嵌套入口）",
              "冒烟没造 node_modules —— Node 分支形同虚设")
        check("os.symlink" in sps,
              "冒烟夹具造了 .bin 符号链接（npm 真实产物形态）",
              "夹具没有符号链接 —— MANIFEST 那条规则就没被真跑过")
        check("WinError" in sps or "建不了符号链接" in sps,
              "建不了链接时明确降级并说明由 CI 覆盖",
              "没处理「宿主建不了符号链接」—— 本机会静默变成假通过")
        check("relative=True" in sps and "relative=False" in sps,
              "冒烟对「相对 / 绝对」两种传参各跑一遍",
              "冒烟只跑一种传参 —— 绝对/相对混用这类 bug 会漏")
        check("self_test_root_cause" in sps,
              "冒烟内置反向验证（植回 bug 必须被抓）",
              "冒烟没有自证 —— 「通过」可能只是没执行到那条路径")
        check("self_test_verifier" in sps,
              "冒烟反向验证体检脚本本身（构造坏树/好树）",
              "没有验证体检脚本 —— 规则写错了也没人知道")
        check("self_test_symlink_skip" in sps,
              "冒烟反向验证「清单跳过符号链接」这条修复",
              "没有这条自证 —— 修复被回退时没人拦")
        # 体检必须校验「清单条目在 tar 里都是普通文件」
        check("MANIFEST 登记了" in sps and "reg" in sps,
              "冒烟直接比对 MANIFEST 与 tar 成员类型",
              "冒烟没比对 tar 成员类型 —— 符号链接问题只能靠体检脚本间接兜")

    # stage_pack 的路径归一：resolve_node_entry() 返回绝对路径，CLI 传的却
    # 可能是相对路径，不归一就 relative_to 崩。这是 bug #2 的正面钉死。
    if bb.is_file():
        src = read(bb)
        i = src.find("def stage_pack(")
        body = src[i:i + 1400] if i >= 0 else ""
        for expr, what in (("Path(args.src).resolve()", "--src"),
                           ("Path(args.node_src).resolve()", "--node-src"),
                           ("Path(args.out).resolve()", "--out")):
            check(expr in body,
                  f"stage_pack 把 {what} 归一成绝对路径",
                  f"stage_pack 缺少 {expr!r} —— CLI 传相对路径时 relative_to 会崩"
                  "（CI run 35415356313）")

    # ── 9. 文件清单一致性 ──
    print("\n· 包内容一致性")
    if (bb.is_file()):
        src = read(bb)
        for p in ("requirements-extras.txt", "requirements-extras-heavy.txt",
                  "requirements-mcp.txt", "skills-official.txt"):
            check((EXTRAS / p).is_file(),
                  f"{p} 存在（会被打进去）", f"{p} 不存在")
        check("requirements-mcp.txt" in src or "requirements-mcp" in src,
              "构建器会读 MCP 需求文件", "构建器没读 MCP 需求文件")

    print()
    if FAIL:
        print(f"❌ 增强包静态自检失败（{len(FAIL)} 项）")
        return 1
    print("✅ 增强包静态自检通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
