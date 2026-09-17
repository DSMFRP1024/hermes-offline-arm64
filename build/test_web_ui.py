#!/usr/bin/env python3
"""锁死「dashboard 前端必须预编译进离线包」这条不变量。

为什么值得单独立一个门 ——
上游 `hermes dashboard` 在找不到前端 dist 时会走：

    _do_build_web_ui() -> npm install --prefer-offline -> npm run build

目标机是零网络信创机，npm 取不到 registry，于是
`_report_web_build_failure(..., fatal=True)` 直接 `sys.exit(1)`。
症状是"包装好了、命令也在、但控制台打不开"，而且**构建日志里全绿** ——
这正是本仓库反复强调的那类"日志看不出来"的坑（另见 glibc 基线那条）。

所以这里用纯文本断言把三件事钉住：
  1. 构建器确实调用了 build_web_ui，且**排在 pack_repo 之前**
     （排在后面 = dist 不进源码快照 = 白编）；
  2. install.sh 确实补写了 build stamp
     （只放 dist 不写戳，运行时仍判"需要重建"）；
  3. CI 确实断言了 web_dist/index.html 在包内。

顺带校验 install.sh 的步骤编号连续无缺号 —— 插步骤时最容易漏改号。

纯静态、无网络、毫秒级，可以进 CI 前置门。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD_PY = ROOT / "build" / "build_bundle.py"
INSTALL_SH = ROOT / "target" / "install.sh"
CHECK_ENV_SH = ROOT / "target" / "check-env.sh"
WORKFLOW = ROOT / ".github" / "workflows" / "build-offline-bundle.yml"

FAILS: list[str] = []
CHECKS = 0


def check(cond: bool, label: str, detail: str = "") -> None:
    global CHECKS
    CHECKS += 1
    if cond:
        print(f"  ✓ {label}")
    else:
        print(f"  ✗ {label}" + (f"\n      {detail}" if detail else ""))
        FAILS.append(label)


def read(p: Path) -> str:
    if not p.is_file():
        return ""
    return p.read_text(encoding="utf-8", errors="replace")


def main() -> int:
    print("=" * 64)
    print("  Web UI 预编译不变量测试")
    print("=" * 64)

    build = read(BUILD_PY)
    install = read(INSTALL_SH)
    cenv = read(CHECK_ENV_SH)
    wf = read(WORKFLOW)

    # ── 1. 构建器 ──
    print("\n[1] build_bundle.py")
    check("def build_web_ui(" in build, "定义了 build_web_ui()")
    check("with_web_ui" in build, "Cfg 有 with_web_ui 开关",
          "离线包默认必须带前端，开关只为本地迭代保留")
    check('"--skip-web-ui"' in build, "CLI 暴露了 --skip-web-ui",
          "缺了它，本地想跳过前端就没法快速迭代")

    # 调用点必须在 build() 里，且**先于** pack_repo。
    # 这是本文件最核心的一条：顺序错了 dist 就进不了源码快照。
    call = build.find("build_web_ui(cfg, env)")
    pack = build.find("pack_repo(cfg, repo_tgz)")
    check(call != -1, "build() 里调用了 build_web_ui(cfg, env)")
    check(pack != -1, "build() 里调用了 pack_repo(cfg, repo_tgz)")
    if call != -1 and pack != -1:
        check(call < pack, "build_web_ui 排在 pack_repo 之前",
              "排在后面 = 前端产物不进源码快照，包还是缺 dist")

    # 产物路径必须和上游 `_web_dist_dir()` 一致：
    #   vite.config.ts 的 outDir 是 "../hermes_cli/web_dist"
    check('"hermes_cli" / "web_dist"' in build,
          "产物路径写的是 hermes_cli/web_dist（与上游 vite outDir 一致）")
    # 构建命令要真的能跑起来：走 workspace，依赖才解析得到（web 的依赖被 hoist 到根）
    check('"run", "build", "--workspace", "web"' in build,
          "用 npm run build --workspace web 触发编译")
    check('"index.html"' in build, "校验了 dist/index.html 存在")

    # ── 2. install.sh ──
    print("\n[2] target/install.sh")
    check("_compute_web_ui_content_hash" in install,
          "调用了上游 _compute_web_ui_content_hash 写戳")
    check("_web_ui_stamp_path" in install, "解析了上游的戳路径（$HERMES_HOME/...）")
    check("hermes_cli/web_dist" in install, "检查了预编译前端目录")
    check("--skip-build" in install, "给了 build stamp 失败时的兜底提示")
    check("STAMP_RC" in install, "写戳失败不会让整条安装失败（只降级告警）")

    # 步骤编号：必须连续无缺号。插步骤忘改号会让日志读起来自相矛盾。
    # 注意用 \s* 而不是 ^：步骤 1 的 log_step 在 if/else 两个分支里，
    # 带 4 空格缩进，锚死在行首会漏掉它，于是"缺号"是假警报。
    nums = [int(m.group(1)) for m in re.finditer(r'^\s*log_step "(\d+)\.', install, re.M)]
    uniq = sorted(set(nums))
    check(uniq != [], "解析到 log_step 编号")
    check(uniq == list(range(uniq[0], uniq[-1] + 1)),
          "install.sh 步骤编号连续、无缺号", f"实际: {uniq}")
    check(uniq[0] == 0, "步骤从 0 起", f"实际: {uniq}")
    check(uniq[-1] >= 10, "收尾步骤编号 >= 10（说明 Web UI 步骤已并入编号）",
          f"实际: {uniq}")

    # ── 3. check-env.sh ──
    print("\n[3] target/check-env.sh")
    check("web_dist/index.html" in cenv, "预检会确认前端产物在包内")
    check("--skip-web-ui" in cenv, "预检给出重打包指引")

    # ── 4. CI 工作流 ──
    print("\n[4] .github/workflows/build-offline-bundle.yml")
    check("hermes_cli/web_dist/index.html" in wf, "校验步骤断言了 web_dist/index.html")
    check("repo/hermes-agent-src.tar.gz" in wf, "断言钻进了嵌套的源码包")
    check("dashboard 前端" in wf, "结果摘要里体现了前端状态")

    print("\n" + "-" * 64)
    if FAILS:
        print(f"  ✗ 失败 {len(FAILS)} / 共 {CHECKS}")
        for f in FAILS:
            print(f"      - {f}")
        print("-" * 64)
        return 1
    print(f"  ✅ 通过 {CHECKS} / 共 {CHECKS}")
    print("-" * 64)
    return 0


if __name__ == "__main__":
    sys.exit(main())
