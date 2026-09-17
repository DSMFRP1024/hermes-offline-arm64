#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""锁死「Hermes Desktop（Electron 壳）必须预打包进离线包」这条不变量。

和 test_web_ui.py 是同一个主题（离线机上不许现场构建），但触发路径不同：

    `hermes desktop` 找不到 unpacked 产物时 → npm ci
        → electron-builder → `@electron/get` 下载 Electron 运行时
        → 目标机零网络，必然失败

而且它比 dashboard 更隐蔽：dashboard 失败会 `sys.exit(1)`，`hermes desktop`
失败时你看到的可能只是"窗口没弹出来"。所以这里把整条链路上的
**六个关键决策**钉成静态断言：

  1. 打包方式必须是 `npm run pack`（= `builder --dir`），
     **不能**是 AppImage/deb/rpm。后者要调 fpm，而 electron-builder 分发的
     fpm 只有 x86_64 —— 在 arm64 构建机上必然失败。
  2. 顺序：`build_desktop` 必须排在 `fetch_node_modules` **之后**
     （要拿它装出来的 vite/esbuild），且排在 `pack_repo` **之前**
     （unpacked 树要靠 REPO_EXCLUDES 挡在源码快照外）。
  3. node-pty 必须重编到 Electron ABI。npm ci 编出来的是 **Node ABI**
     （Node 24 = 137），Electron 40 要 **Electron ABI**（143）；
     不匹配时终端面板 NODE_MODULE_VERSION mismatch 后整块不可用。
  4. `apps/desktop/release` 必须在 REPO_EXCLUDES 里：unpacked 树有 290 MiB
     量级，它单独打成 `desktop/*.tar.gz`；漏排 = 包里存两遍。
  5. install.sh 必须解压产物并补写 desktop build stamp，且 `sourceMode`
     写 **false** —— 上游 `_stamp_is_current()` 会比对它，写 true 等于没写。
  6. check-env.sh / CI 必须能在装之前、构建之后分别发现"桌面版没进包"。

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
    print("  Hermes Desktop（Electron）预打包不变量测试")
    print("=" * 64)

    build = read(BUILD_PY)
    install = read(INSTALL_SH)
    cenv = read(CHECK_ENV_SH)
    wf = read(WORKFLOW)

    # ── 1. 开关与 CLI ──
    print("\n[1] build_bundle.py：开关与 CLI")
    check("def build_desktop(" in build, "定义了 build_desktop()")
    check("with_desktop" in build, "Cfg 有 with_desktop 开关",
          "用户已确认目标机有图形桌面，默认必须带")
    check('"--skip-desktop"' in build, "CLI 暴露了 --skip-desktop",
          "缺了它，本地想跳过桌面版就没法快速迭代")
    check("with_desktop=not args.skip_desktop" in build,
          "CLI 默认开启桌面版（--skip-desktop 才关）",
          "默认值没接上，用户装完会发现桌面版不见了")

    # ── 2. 打包方式：--dir，不是安装器 ──
    print("\n[2] 打包方式：unpacked 树，不碰 fpm")
    check('"run", "pack"' in build, "用 npm run pack（= builder --dir）",
          "pack 只产出 unpacked 目录；dist:linux 会去调 fpm")
    check('"--linux"' not in build, "没有传 --linux（安装器 target）",
          "AppImage/deb/rpm 都要调 fpm，而 electron-builder 的 fpm 只有 x86_64")
    check("fpm" in build, "注释里记录了 fpm 只有 x86_64 这个坑",
          "这个坑不写在旁边，下次很容易有人改回 dist:linux")
    check("DESKTOP_UNPACKED_DIRS = (\"linux-arm64-unpacked\", \"linux-unpacked\")" in build,
          "认 linux-arm64-unpacked / linux-unpacked 两个落点",
          "上游 _desktop_packaged_executable_in() 认的就是这两个")
    check("DESKTOP_EXE_NAMES" in build, "可执行文件名同时认 hermes / Hermes",
          "productName 是 Hermes，binary 名是 hermes，两者都可能出现")

    # ── 3. 顺序：node_modules → desktop → pack_repo ──
    print("\n[3] 调用顺序（顺序错了就是白编 / 白胖）")
    nm = build.find("fetch_node_modules(cfg, env, node_modules)")
    desk = build.find("build_desktop(cfg, env, desktop_out)")
    pack = build.find("pack_repo(cfg, repo_tgz)")
    check(nm != -1, "build() 里调用了 fetch_node_modules(cfg, env, node_modules)")
    check(desk != -1, "build() 里调用了 build_desktop(cfg, env, desktop_out)")
    check(pack != -1, "build() 里调用了 pack_repo(cfg, repo_tgz)")
    if nm != -1 and desk != -1:
        check(nm < desk, "build_desktop 排在 fetch_node_modules 之后",
              "它要用 fetch_node_modules 装出来的 vite/esbuild/electron-builder")
    if desk != -1 and pack != -1:
        check(desk < pack, "build_desktop 排在 pack_repo 之前",
              "unpacked 树要靠 REPO_EXCLUDES 挡在源码快照外，跳过顺序会漏挡")
    web = build.find("build_web_ui(cfg, env)")
    if web != -1 and desk != -1:
        check(web < desk, "Web UI 先于桌面版（步骤号 7 → 8）",
              "两者都吃 node_modules，先编前端再打桌面，日志顺序才不会骗人")

    # ── 4. Electron ABI ──
    print("\n[4] node-pty 必须重编到 Electron ABI")
    check("rebuild-native.mjs" in build, "调用了上游 scripts/rebuild-native.mjs",
          "Electron 40 = ABI 143 / Node 24 = ABI 137，不重编就 NODE_MODULE_VERSION mismatch")
    call_argv = '["node", "scripts/rebuild-native.mjs", "arm64"]'
    check(call_argv in build, "重编时显式传 arm64")
    # 定位**调用点**（不是文档字符串里那次提及），再看它后面是不是 check=False
    rn = build.find(call_argv)
    pk = build.find('"run", "pack"')
    if rn != -1 and pk != -1:
        check(rn < pk, "rebuild 发生在 pack 之前",
              "pack 会把 node_modules/node-pty 一起收进 app.asar.unpacked")
        check("check=False" in build[rn:rn + 500],
              "rebuild 失败只降级告警（不致命的失败别作废整个包）",
              "内嵌终端不可用，但主界面仍要能开")

    # ── 5. 依赖清单与白屏护栏 ──
    print("\n[5] 桌面版构建依赖（少装包是静默的）")
    check("DESKTOP_TOOLCHAIN_REQUIRED" in build, "桌面版构建链关键包清单存在")
    for pkg in ("electron", "electron-builder", "@electron/rebuild", "vite"):
        check(f'"{pkg}"' in build, f"清单含 {pkg}")
    check("def assert_desktop_toolchain(" in build, "定义了 assert_desktop_toolchain()")
    check("assert_desktop_toolchain(repo)" in build,
          "装完显式校验桌面版构建链依赖",
          "npm 装没装包完全静默，要到 vite/esbuild 才炸")
    check("react != react_dom" in build,
          "校验 react / react-dom 同版本（防 React #527 白屏）",
          "版本不一致时 npm 沉默，渲染出来是全白窗口，最难查")

    # 反向：workspace 子目录里禁止二次安装（与 web 那条同源）
    check('cwd=repo / "ui-tui"' not in build,
          "没有在 ui-tui/ 里二次安装",
          "它会把根 node_modules 剪成只剩 ui-tui 子树，桌面版依赖随之消失")
    check('"apps/desktop", "apps/shared"' in build,
          "fetch_node_modules 的 workspace 列表含 apps/desktop + apps/shared",
          "apps/desktop 的依赖被 hoist 到根，不装就缺 electron/electron-builder")
    check('"apps/desktop/node_modules"' in build,
          "node_modules tarball 收集了 apps/desktop/node_modules",
          "版本冲突的包会落在子目录里，不收就等于丢包")
    check('"apps/shared/node_modules"' in build,
          "node_modules tarball 收集了 apps/shared/node_modules")

    # Electron 运行时本体不该进"给目标机 CLI"的 node_modules tarball
    check("def _nm_tarball_filter(" in build,
          "有 tarball 过滤器（排掉 electron/dist 运行时本体）",
          "node_modules/electron/dist 解包 200 MiB，CLI 永远不用它")

    # ── 6. 不重复打包 ──
    print("\n[6] unpacked 树不许在源码快照里存第二份")
    m = re.search(r"REPO_EXCLUDES = \(([^)]*)\)", build, re.S)
    check(m is not None, "找到 REPO_EXCLUDES 定义")
    if m:
        check('"apps/desktop/release"' in m.group(1),
              "REPO_EXCLUDES 排除了 apps/desktop/release",
              "290 MiB 的 unpacked 树会在包里存两遍（外层 desktop/ + 内层源码快照）")
        check('"node_modules"' in m.group(1),
              "REPO_EXCLUDES 仍然排除了 node_modules")

    check('DESKTOP_TARBALL_NAME = "hermes-desktop-linux-arm64.tar.gz"' in build,
          "桌面版单独打成 desktop/hermes-desktop-linux-arm64.tar.gz")
    check("desktop: Path | None = None" in build,
          "assemble_bundle() 接受 desktop 参数")
    check('(desktop, "desktop")' in build,
          "assemble_bundle() 把 tarball 拷进 bundle/desktop/")
    check('"desktop_prebuilt": cfg.with_desktop' in build,
          "build-info.json 记录了 desktop_prebuilt")

    # ── 7. install.sh ──
    print("\n[7] target/install.sh")
    check("desktop/*.tar.gz" in install, "从 bundle/desktop/*.tar.gz 解压")
    check("DESKTOP_RELEASE" in install, "解压目标是 apps/desktop/release（上游认的落点）")
    check("_compute_desktop_content_hash" in install,
          "调用了上游 _compute_desktop_content_hash 写戳")
    check("_desktop_stamp_path" in install, "解析了上游的戳路径（$HERMES_HOME/...）")
    check("desktop-build-stamp.json" in install, "写的是 desktop-build-stamp.json")
    check('"sourceMode": False' in install,
          "stamp 里 sourceMode 写 false",
          "_stamp_is_current() 会比对 sourceMode；写 true 等于没写，仍会去 npm 构建")
    check("chrome-sandbox" in install, "处理了 Electron 的 setuid sandbox",
          "root 下不加 --no-sandbox 会直接拒绝启动")
    check("--skip-build" in install, "给了 stamp 失败时的兜底提示（--skip-build）")
    check("STAMP_RC" in install, "写戳失败不会让整条安装失败（只降级告警）")
    check("DISPLAY" in install and "WAYLAND_DISPLAY" in install,
          "无图形会话时给出明确提示（而不是让用户以为装坏了）")

    # 步骤编号连续；且 Web UI(9) 在 Desktop(10) 之前。
    nums = [int(m.group(1)) for m in re.finditer(r'^\s*log_step "(\d+)\.', install, re.M)]
    uniq = sorted(set(nums))
    check(uniq != [], "解析到 log_step 编号")
    check(uniq == list(range(uniq[0], uniq[-1] + 1)),
          "install.sh 步骤编号连续、无缺号", f"实际: {uniq}")
    check(9 in uniq and 10 in uniq and 10 > 9,
          "Web UI(9) 之后才是 Desktop(10)", f"实际: {uniq}")
    check(uniq[-1] >= 12, "收尾步骤编号 >= 12（Desktop 已并入编号）", f"实际: {uniq}")

    # ── 8. check-env.sh ──
    print("\n[8] target/check-env.sh")
    check("desktop/*.tar.gz" in cenv, "预检会确认桌面版产物在包内")
    check("--skip-desktop" in cenv, "预检给出重打包指引")
    check("libgtk-3.so.0" in cenv, "预检会查桌面版图形库（GTK3 等）")
    check("DISPLAY" in cenv, "预检会提示需要图形会话")

    # ── 9. CI 工作流 ──
    print("\n[9] .github/workflows/build-offline-bundle.yml")
    check("build/test_desktop.py" in wf,
          "静态门里挂了 test_desktop.py",
          "不挂进去，这条不变量在 CI 上等于没有")
    check("test_desktop.py" in wf.split("校验产物")[0],
          "test_desktop.py 在拉镜像之前就跑了（前置门）",
          "放到构建后面跑，等于让几十分钟的构建白跑")
    check("desktop/hermes-desktop-linux-arm64.tar.gz" in wf,
          "校验步骤断言桌面版 tarball 在包内")
    check("linux-(arm64-)?unpacked" in wf,
          "断言钻进了 desktop tarball，确认 unpacked 可执行文件存在",
          "只列外层 tar 是不够的 —— desktop 产物在它自己那层 tarball 里")
    check("apps/desktop/release" in wf,
          "断言源码快照里没有重复的 unpacked 树")
    check("桌面版" in wf, "结果摘要里体现了桌面版状态")

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
