#!/usr/bin/env bash
# =============================================================================
# Hermes Agent 一键离线安装（Linux ARM64 / 信创）
# =============================================================================
#
# 目标机**全程零网络**：不解压外网、不调 pip 索引、不碰 npm registry、
# 不下 Chromium。所有东西（Python 运行时、wheel、Node、node_modules、
# 浏览器、rg/ffmpeg）都在本目录里。
#
# 用法：
#     cd hermes-offline-arm64
#     bash check-env.sh          # 可选：先做只读预检
#     sudo bash install.sh       # 一键安装
#
# 常用参数：
#     --dir PATH          安装目录（默认 root: /usr/local/lib/hermes-agent）
#     --hermes-home PATH  数据目录（默认 ~/.hermes）
#     --force             重建虚拟环境 / 重新解压已存在的运行时
#     --skip-browser      不铺 Chromium（浏览器工具将不可用）
#     --no-verify         跳过 MANIFEST.sha256 校验（介质可信时可省 1-2 分钟）
#     --uninstall         卸载（保留数据目录）
#     -h | --help
#
# 设计要点（改脚本前先读）：
#   1. 不用系统 Python。信创机常见 3.8/3.9，而 Hermes 要求 >=3.11,<3.14；
#      且系统 Python 动不得。所以自带 runtime/python 独立运行时。
#   2. 全程 pip --no-index：任何一次索引访问在离线机上都会以超时挂住，
#      而不是快速失败，所以把 --no-index 写死在封装函数里。
#   3. 用 `pip install -e .` 而不是装 wheel：上游 setup.py 明确禁止在 Nix
#      之外构建 wheel/sdist（bdist_wheel/sdist 直接 raise），但 editable
#      走 build_editable，不受该守卫影响。这也是上游 install.sh 的做法。
#   4. node_modules 是**预构建**的：node-pty 没有 Linux 预编译包，
#      每次安装都要 node-gyp 编译，而信创机通常没有 make/gcc。
#   5. Web UI（dashboard 前端）同样是**预构建**的：上游在找不到 dist 时会
#      去跑 npm install && npm run build，这在离线机上基本必挂。构建机先把
#      dist 编好，本脚本再补写 build stamp，`hermes dashboard` 才开箱即用。
# =============================================================================

set -euo pipefail

BUNDLE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── 颜色 ──
if [ -t 1 ]; then
    C_RED='\033[0;31m'; C_GREEN='\033[0;32m'; C_YELLOW='\033[0;33m'
    C_CYAN='\033[0;36m'; C_BOLD='\033[1m'; C_OFF='\033[0m'
else
    C_RED=''; C_GREEN=''; C_YELLOW=''; C_CYAN=''; C_BOLD=''; C_OFF=''
fi

log_info()  { echo -e "${C_CYAN}→${C_OFF} $*"; }
log_ok()    { echo -e "${C_GREEN}✓${C_OFF} $*"; }
log_warn()  { echo -e "${C_YELLOW}⚠${C_OFF} $*"; }
log_err()   { echo -e "${C_RED}✗${C_OFF} $*" >&2; }
log_step()  { echo ""; echo -e "${C_BOLD}── $* ──${C_OFF}"; }
die()       { log_err "$*"; exit 1; }

# ── 默认值 ──
PY_VERSION="3.11"
GLIBC_MINOR_REQUIRED=28
FORCE=false
SKIP_BROWSER=false
VERIFY=true
UNINSTALL=false
INSTALL_DIR=""
HERMES_HOME="${HERMES_HOME:-}"
ASSUME_ROOT_LAYOUT=""

# --help 直接回放文件头注释（到设计要点结束为止），避免另写一份会漂移的说明。
usage() { sed -n '3,36p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0; }

while [ $# -gt 0 ]; do
    case "$1" in
        --dir)          INSTALL_DIR="$2"; shift 2 ;;
        --hermes-home)  HERMES_HOME="$2";  shift 2 ;;
        --user)         ASSUME_ROOT_LAYOUT=false; shift ;;
        --force)        FORCE=true; shift ;;
        --skip-browser) SKIP_BROWSER=true; shift ;;
        --no-verify)    VERIFY=false; shift ;;
        --uninstall)    UNINSTALL=true; shift ;;
        -h|--help)      usage ;;
        *) die "未知参数: $1（用 --help 看用法）" ;;
    esac
done

IS_ROOT=false
[ "$(id -u)" -eq 0 ] && IS_ROOT=true

if [ -z "$INSTALL_DIR" ]; then
    if [ "${ASSUME_ROOT_LAYOUT:-auto}" = "true" ] || { [ "$IS_ROOT" = true ] && [ "${ASSUME_ROOT_LAYOUT:-auto}" != "false" ]; }; then
        INSTALL_DIR="/usr/local/lib/hermes-agent"
        LINK_DIR="/usr/local/bin"
    else
        INSTALL_DIR="$HOME/.hermes/hermes-agent"
        LINK_DIR="$HOME/.local/bin"
    fi
fi

if [ -z "$HERMES_HOME" ]; then
    if [ "$IS_ROOT" = true ]; then HERMES_HOME="/root/.hermes"; else HERMES_HOME="$HOME/.hermes"; fi
fi
[ -z "${LINK_DIR:-}" ] && { [ "$IS_ROOT" = true ] && LINK_DIR="/usr/local/bin" || LINK_DIR="$HOME/.local/bin"; }

VENV="$INSTALL_DIR/venv"
PY_DIR="$INSTALL_DIR/runtime/python"
NODE_DIR="$HERMES_HOME/node"

# =============================================================================
# 卸载
# =============================================================================
if [ "$UNINSTALL" = true ]; then
    log_step "卸载"
    for cmd in hermes hermes-agent hermes-acp; do
        rm -f "$LINK_DIR/$cmd"
    done
    for d in "$VENV" "$INSTALL_DIR"; do
        [ -e "$d" ] && rm -rf "$d" && log_ok "已删除 $d"
    done
    log_ok "命令入口已移除；数据目录保留在 $HERMES_HOME"
    log_info "如需彻底清除：rm -rf $HERMES_HOME"
    exit 0
fi

echo ""
echo -e "${C_BOLD}┌─────────────────────────────────────────────────────┐${C_OFF}"
echo -e "${C_BOLD}│   ☤ Hermes Agent 离线安装（ARM64 / 离线）           │${C_OFF}"
echo -e "${C_BOLD}└─────────────────────────────────────────────────────┘${C_OFF}"
echo ""

# =============================================================================
# 0. 前置检查
# =============================================================================
log_step "0. 环境预检"

ARCH="$(uname -m)"
case "$ARCH" in
    aarch64|arm64) log_ok "架构 $ARCH" ;;
    *) die "本包只支持 aarch64/arm64，当前是 $ARCH" ;;
esac

if command -v getconf >/dev/null 2>&1; then
    GLIBC_RAW="$(getconf GNU_LIBC_VERSION 2>/dev/null || echo '')"
    GLIBC_MAJOR="$(printf '%s' "$GLIBC_RAW" | sed -n 's/.* \([0-9]*\)\.\([0-9]*\).*/\1/p')"
    GLIBC_MIN="$(printf '%s' "$GLIBC_RAW" | sed -n 's/.* \([0-9]*\)\.\([0-9]*\).*/\2/p')"
    if [ -n "$GLIBC_MAJOR" ]; then
        if [ "$GLIBC_MAJOR" -lt 2 ] || { [ "$GLIBC_MAJOR" -eq 2 ] && [ "$GLIBC_MIN" -lt "$GLIBC_MINOR_REQUIRED" ]; }; then
            log_warn "系统 glibc $GLIBC_MAJOR.$GLIBC_MIN 低于本包基线 2.$GLIBC_MINOR_REQUIRED"
            log_warn "很多原生扩展（cryptography / numpy / Pillow）会 import 失败。"
            log_warn "需要重打一个 --glibc 2.17 的包。"
            if [ -t 0 ]; then
                read -r -p "仍要继续？[y/N] " _ans
                case "${_ans:-n}" in y|Y|yes|YES) ;; *) exit 1 ;; esac
            fi
        else
            log_ok "glibc $GLIBC_MAJOR.$GLIBC_MIN"
        fi
    fi
else
    log_warn "没有 getconf，跳过 glibc 检查"
fi

# 磁盘空间：包本体 + 解压后的 venv / node_modules
BUNDLE_MB="$(du -sm "$BUNDLE_DIR" 2>/dev/null | awk '{print $1}' || echo 0)"
NEED_MB=$(( BUNDLE_MB * 3 + 500 ))
TARGET_FS="$(df -Pk "$INSTALL_DIR" 2>/dev/null | awk 'NR==2{print $4}' || echo 0)"
TARGET_FS=$(( TARGET_FS / 1024 ))
if [ "$TARGET_FS" -gt 0 ] && [ "$TARGET_FS" -lt "$NEED_MB" ]; then
    die "$INSTALL_DIR 所在分区只剩 ${TARGET_FS}MB，估计需要 ${NEED_MB}MB"
fi
log_ok "磁盘空间 可用 ${TARGET_FS}MB / 预计需要 ${NEED_MB}MB"

for f in requirements.lock.txt repo/hermes-agent-src.tar.gz; do
    [ -e "$BUNDLE_DIR/$f" ] || die "离线包不完整：缺少 $f"
done

# =============================================================================
# 1. 校验清单
# =============================================================================
if [ "$VERIFY" = true ] && [ -f "$BUNDLE_DIR/MANIFEST.sha256" ]; then
    log_step "1. 校验包完整性"
    if (cd "$BUNDLE_DIR" && sha256sum -c --quiet MANIFEST.sha256 2>/dev/null); then
        log_ok "MANIFEST.sha256 全部通过"
    else
        log_err "校验失败 —— 介质可能损坏，或包被改动过"
        log_info "详情：cd $BUNDLE_DIR && sha256sum -c MANIFEST.sha256 | grep -v ': OK'"
        die "拒绝在未通过校验的包上安装（--no-verify 可跳过）"
    fi
else
    log_step "1. 校验包完整性（已跳过）"
fi

# =============================================================================
# 2. Python 运行时
# =============================================================================
log_step "2. 部署独立 Python 运行时 $PY_VERSION"

if [ -x "$PY_DIR/bin/python3" ] && [ "$FORCE" != true ]; then
    log_ok "运行时已存在，复用 ($("$PY_DIR/bin/python3" --version 2>&1))"
else
    PY_TGZ="$(ls "$BUNDLE_DIR"/runtime/cpython-"$PY_VERSION".*-aarch64-unknown-linux-gnu-install_only.tar.gz 2>/dev/null | head -1 || true)"
    [ -n "$PY_TGZ" ] || die "找不到 runtime/cpython-$PY_VERSION.*-aarch64-unknown-linux-gnu-install_only.tar.gz"
    mkdir -p "$INSTALL_DIR/runtime"
    [ "$FORCE" = true ] && rm -rf "$PY_DIR"
    log_info "解压 $(basename "$PY_TGZ") ..."
    tar -xzf "$PY_TGZ" -C "$INSTALL_DIR/runtime"
    [ -x "$PY_DIR/bin/python3" ] || die "解压后没找到 $PY_DIR/bin/python3"
    log_ok "运行时就绪 ($("$PY_DIR/bin/python3" --version 2>&1))"
fi

# =============================================================================
# 3. 部署源码
# =============================================================================
log_step "3. 部署 Hermes 源码到 $INSTALL_DIR"

mkdir -p "$INSTALL_DIR"
if [ -f "$INSTALL_DIR/run_agent.py" ] && [ "$FORCE" != true ]; then
    log_ok "源码已存在，跳过（--force 可强制覆盖）"
else
    # 只覆盖代码文件，不动 runtime/ venv/ 这些同级目录
    tar -xzf "$BUNDLE_DIR/repo/hermes-agent-src.tar.gz" \
        --strip-components=1 -C "$INSTALL_DIR" \
        --exclude='runtime' --exclude='venv'
    [ -f "$INSTALL_DIR/run_agent.py" ] || die "源码解压异常，缺少 run_agent.py"
    log_ok "源码就绪（$(find "$INSTALL_DIR" -maxdepth 1 -name '*.py' | wc -l) 个顶层模块）"
fi

# =============================================================================
# 4. 虚拟环境 + 离线装包
# =============================================================================
log_step "4. 建立虚拟环境并离线安装依赖"

PY_BIN="$PY_DIR/bin/python3"

if [ -d "$VENV" ] && [ "$FORCE" = true ]; then
    log_info "--force：删除已有 venv"
    rm -rf "$VENV"
fi

if [ ! -x "$VENV/bin/python" ]; then
    log_info "创建虚拟环境..."
    "$PY_BIN" -m venv "$VENV" || die "venv 创建失败"
fi
[ -x "$VENV/bin/python" ] || die "$VENV/bin/python 不存在"
VPY="$VENV/bin/python"

if [ ! -x "$VENV/bin/pip" ] && ! "$VPY" -m pip --version >/dev/null 2>&1; then
    die "venv 里没有 pip —— 自带运行时可能不完整（需要 ensurepip）。"
fi

# 离线安装的封装：写死 --no-index，避免任何索引访问在离线机上以超时形式挂住。
WHEEL_DIRS=(--find-links "$BUNDLE_DIR/wheels")
[ -d "$BUNDLE_DIR/wheels-prebuilt" ] && WHEEL_DIRS+=(--find-links "$BUNDLE_DIR/wheels-prebuilt")

pip_offline() {
    env PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_INPUT=1 PIP_NO_INDEX=1 \
        "$VPY" -m pip "$@" --no-index "${WHEEL_DIRS[@]}"
}

STAMP="$VENV/.hermes-offline-stamp"
LOCK_HASH="$(sha256sum "$BUNDLE_DIR/requirements.lock.txt" | awk '{print $1}')"
if [ -f "$STAMP" ] && [ "$(cat "$STAMP")" = "$LOCK_HASH" ] && [ "$FORCE" != true ]; then
    log_ok "依赖已按当前 lock 装好，跳过（--force 可重装）"
else
    log_info "安装 $(grep -c -v '^#' "$BUNDLE_DIR/requirements.lock.txt" || true) 条依赖（全程 --no-index）..."
    pip_offline install -r "$BUNDLE_DIR/requirements.lock.txt" --no-build-isolation \
        || die "依赖安装失败。查 $VENV/../ 日志，或先跑 bash check-env.sh"
    log_info "以 editable 方式注册 hermes 本体..."
    # 上游 setup.py 只拦 bdist_wheel/sdist；editable 走 build_editable，不受影响。
    ( cd "$INSTALL_DIR" && pip_offline install -e . --no-deps --no-build-isolation ) \
        || die "editable 安装失败"
    printf '%s' "$LOCK_HASH" > "$STAMP"
    log_ok "依赖安装完成"
fi

"$VPY" - <<'PYEOF' || die "关键依赖 import 失败，包可能与本机 glibc 不匹配"
import sys
mods = ["cryptography", "pydantic_core", "PIL", "psutil", "yaml", "httpx", "openai"]
bad = []
for m in mods:
    try:
        __import__(m)
    except Exception as e:
        bad.append(f"{m}: {type(e).__name__}: {e}")
if bad:
    print("以下原生/关键依赖导入失败：")
    for b in bad:
        print("  -", b)
    sys.exit(1)
print(f"  ✓ {len(mods)} 个关键依赖 import 正常")
PYEOF

# =============================================================================
# 5. Node.js
# =============================================================================
log_step "5. 部署 Node.js"

NODE_TGZ="$(ls "$BUNDLE_DIR"/runtime/node-v*-linux-arm64.tar.xz 2>/dev/null | head -1 || true)"
if [ -z "$NODE_TGZ" ]; then
    log_warn "包内没有 Node，浏览器工具与 TUI 将不可用"
else
    if [ -x "$NODE_DIR/bin/node" ] && [ "$FORCE" != true ]; then
        log_ok "Node 已存在 ($("$NODE_DIR/bin/node" --version 2>&1))"
    else
        rm -rf "$NODE_DIR"
        mkdir -p "$HERMES_HOME"
        tar -xJf "$NODE_TGZ" -C "$HERMES_HOME"
        INNER="$(ls -d "$HERMES_HOME"/node-v*-linux-arm64 2>/dev/null | head -1 || true)"
        [ -n "$INNER" ] || die "Node 解压异常"
        mv "$INNER" "$NODE_DIR"
        log_ok "Node $("$NODE_DIR/bin/node" --version 2>&1) 就绪"
    fi

    mkdir -p "$LINK_DIR" "$NODE_DIR/etc"
    for b in node npm npx; do
        ln -sf "$NODE_DIR/bin/$b" "$LINK_DIR/$b"
    done
    # npm 的全局 prefix 指到 link dir 的父级，否则 npm -g 的产物会落在一个
    # 不在 PATH 上、且每次升级 Node 都会被清掉的位置。
    printf 'prefix=%s\n' "$(dirname "$LINK_DIR")" > "$NODE_DIR/etc/npmrc"
    log_ok "node/npm/npx 已链接到 $LINK_DIR"
fi

# =============================================================================
# 6. 预构建的 node_modules
# =============================================================================
log_step "6. 铺设 node_modules（预构建，无需编译器）"

shopt -s nullglob
NM_ARCHIVES=("$BUNDLE_DIR"/node_modules/*.tar.gz)
shopt -u nullglob
if [ "${#NM_ARCHIVES[@]}" -eq 0 ]; then
    log_warn "包内没有 node_modules，浏览器工具将不可用"
else
    for arc in "${NM_ARCHIVES[@]}"; do
        rel="$(basename "$arc" .tar.gz | sed 's|__|/|g')"
        if [ -d "$INSTALL_DIR/$rel" ] && [ "$FORCE" != true ] && [ -n "$(ls -A "$INSTALL_DIR/$rel" 2>/dev/null)" ]; then
            log_ok "$rel 已存在，跳过"
            continue
        fi
        rm -rf "$INSTALL_DIR/$rel"
        tar -xzf "$arc" -C "$INSTALL_DIR"
        log_ok "$rel 就绪（$(du -sm "$INSTALL_DIR/$rel" 2>/dev/null | awk '{print $1}')MB）"
    done
fi

# node-pty 是唯一的强制原生模块，单独确认一下
PTY_FOUND=""
for cand in "$INSTALL_DIR/node_modules/node-pty" "$INSTALL_DIR/ui-tui/node_modules/node-pty"; do
    if ls "$cand"/build/Release/*.node >/dev/null 2>&1; then PTY_FOUND="$cand"; break; fi
done
if [ -n "$PTY_FOUND" ]; then
    log_ok "node-pty 原生模块就绪"
else
    log_warn "没找到 node-pty 的 .node 产物，PTY/终端功能可能受限"
fi

# =============================================================================
# 7. Playwright Chromium
# =============================================================================
log_step "7. 部署 Playwright Chromium"

PW_HOME="$HERMES_HOME/ms-playwright"
PW_ARC="$(ls "$BUNDLE_DIR"/browsers/ms-playwright-arm64.tar.gz 2>/dev/null | head -1 || true)"

if [ "$SKIP_BROWSER" = true ]; then
    log_info "已按 --skip-browser 跳过"
elif [ -z "$PW_ARC" ]; then
    log_warn "包内没有浏览器缓存，浏览器工具不可用"
else
    if [ -d "$PW_HOME" ] && [ "$FORCE" != true ] && [ -n "$(ls -A "$PW_HOME" 2>/dev/null)" ]; then
        log_ok "浏览器缓存已存在，跳过"
    else
        mkdir -p "$PW_HOME"
        tar -xzf "$PW_ARC" -C "$PW_HOME"
        log_ok "已铺到 $PW_HOME（$(du -sm "$PW_HOME" | awk '{print $1}')MB）"
    fi

    CHROME="$(ls -d "$PW_HOME"/chromium-*/chrome-linux*/chrome 2>/dev/null | head -1 || true)"
    if [ -n "$CHROME" ]; then
        chmod +x "$CHROME" 2>/dev/null || true
        MISSING="$(ldd "$CHROME" 2>/dev/null | awk '/not found/{print $1}' | sort -u || true)"
        if [ -z "$MISSING" ]; then
            log_ok "Chromium 动态库完整"
        else
            log_warn "Chromium 缺少以下系统库，浏览器工具会启动失败："
            printf '%s\n' "$MISSING" | sed 's/^/      /'
            log_info "信创系统上尝试（需已配好本地软件源）："
            log_info "  sudo yum install -y nss nspr libdrm libxkbcommon mesa-libgbm pango cairo alsa-lib atk at-spi2-core cups-libs"
            log_info "  （UOS/Debian 系：sudo apt install -y libnss3 libnspr4 libdrm2 libxkbcommon0 libgbm1 libpango-1.0-0 libcairo2 libasound2 libatk1.0-0 libatk-bridge2.0-0 libcups2）"
        fi
    fi
fi

# =============================================================================
# 8. ripgrep / ffmpeg / uv / 原生扩展
# =============================================================================
log_step "8. 部署辅助工具"

mkdir -p "$HERMES_HOME/bin"
for b in rg ffmpeg ffprobe; do
    if [ -f "$BUNDLE_DIR/bin/$b" ]; then
        install -m 0755 "$BUNDLE_DIR/bin/$b" "$HERMES_HOME/bin/$b"
        log_ok "$b → $HERMES_HOME/bin/$b"
    fi
done

# uv：离线机上装不了任何东西，但 Hermes 的托管 uv 检测路径会去看
# $HERMES_HOME/bin/uv。放一个进去，让 `hermes tools` 之类不去尝试下载。
if [ -f "$BUNDLE_DIR/bin/uv" ]; then
    install -m 0755 "$BUNDLE_DIR/bin/uv" "$HERMES_HOME/bin/uv"
    log_ok "uv → $HERMES_HOME/bin/uv（离线环境下不可用于安装）"
elif [ -x "$BUNDLE_DIR/.tools/uv" ]; then
    install -m 0755 "$BUNDLE_DIR/.tools/uv" "$HERMES_HOME/bin/uv"
    log_ok "uv → $HERMES_HOME/bin/uv"
fi

mkdir -p "$HERMES_HOME/lib"
shopt -s nullglob
for so in "$BUNDLE_DIR"/lib/*.so; do
    install -m 0644 "$so" "$HERMES_HOME/lib/$(basename "$so")"
    log_ok "$(basename "$so") → $HERMES_HOME/lib/（中文分词检索加速）"
done
shopt -u nullglob

# =============================================================================
# 9. Web UI 控制台（预编译，离线直接可起）
# =============================================================================
log_step "9. 部署 Web UI 控制台"

WEB_DIST="$INSTALL_DIR/hermes_cli/web_dist"
if [ -f "$WEB_DIST/index.html" ]; then
    log_ok "预编译前端就位（$(find "$WEB_DIST" -type f | wc -l) 个文件）"

    # 光有 dist 还不够。上游 _web_ui_build_needed() 的判据是
    # 「dist 里有 index.html 或 .vite/manifest.json」**且**
    # 「$HERMES_HOME/web-ui-build-stamp.json 的内容哈希与当前源码树一致」。
    # 那个戳不在仓库里，只能在本机按当前源码树算一次。漏了它，裸跑
    # `hermes dashboard` 仍会判定"需要重建"，接着去跑 npm install —— 而
    # 离线机上 npm 取不到 registry，最终 sys.exit(1)，表现就是"控制台打不开"。
    STAMP_RC=0
    HERMES_HOME="$HERMES_HOME" "$VPY" - "$INSTALL_DIR" <<'PYEOF' || STAMP_RC=$?
import datetime, json, sys
from pathlib import Path

root = Path(sys.argv[1])
try:
    from hermes_cli.main_web_build import _compute_web_ui_content_hash, _web_ui_stamp_path
    stamp = _web_ui_stamp_path()
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.write_text(json.dumps({
        "contentHash": _compute_web_ui_content_hash(root, root / "web"),
        "source": "offline-bundle",
        "builtAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }, indent=2) + "\n", encoding="utf-8")
    print(f"  ✓ build stamp → {stamp}")
except Exception as exc:
    print(f"  ✗ 写 build stamp 失败：{type(exc).__name__}: {exc}")
    sys.exit(1)
PYEOF
    if [ "$STAMP_RC" -eq 0 ]; then
        log_ok "已写入 web-ui-build-stamp.json（dashboard 不会再去碰 npm）"
    else
        log_warn "没能写入 build stamp，裸 \`hermes dashboard\` 可能仍会尝试重建前端"
        log_info "   绕过办法： hermes dashboard --skip-build"
    fi
else
    log_warn "包里没有预编译前端（缺 $WEB_DIST/index.html）"
    log_info "   离线机上 \`hermes dashboard\` 会自动尝试 npm 构建并失败。"
    log_info "   请改用带前端的离线包重装（构建时不要加 --skip-web-ui）。"
fi

# =============================================================================
# 10. 数据目录与配置模板
# =============================================================================
log_step "10. 初始化数据目录"

for d in cron sessions logs pairing hooks image_cache audio_cache memories skills lib bin node; do
    mkdir -p "$HERMES_HOME/$d"
done
log_ok "$HERMES_HOME 目录结构就绪"

# 首次安装时从上游模板生成 config.yaml / .env（已存在则一律不动，绝不覆盖用户配置）
if [ -f "$INSTALL_DIR/cli-config.yaml.example" ] && [ ! -f "$HERMES_HOME/config.yaml" ]; then
    cp "$INSTALL_DIR/cli-config.yaml.example" "$HERMES_HOME/config.yaml"
    log_ok "已生成 $HERMES_HOME/config.yaml（来自上游模板）"
fi
if [ -f "$INSTALL_DIR/.env.example" ] && [ ! -f "$HERMES_HOME/.env" ]; then
    cp "$INSTALL_DIR/.env.example" "$HERMES_HOME/.env"
    chmod 600 "$HERMES_HOME/.env"
    log_ok "已生成 $HERMES_HOME/.env（请填入 API key）"
fi

cat > "$HERMES_HOME/.offline-install" <<EOF
installed_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
bundle=$(basename "$BUNDLE_DIR")
install_dir=$INSTALL_DIR
hermes_home=$HERMES_HOME
mode=offline
EOF

# =============================================================================
# 11. 命令入口
# =============================================================================
log_step "11. 生成 hermes 命令"

mkdir -p "$LINK_DIR"

write_shim() {
    local name="$1" entry="$2"
    local p="$LINK_DIR/$name"
    rm -f "$p"   # 若旧版是符号链接，cat > 会顺着链接覆盖 venv 里的入口，导致自我递归
    cat > "$p" <<EOF
#!/usr/bin/env bash
# Hermes Agent 离线安装入口 —— 由 install.sh 生成
unset PYTHONPATH
unset PYTHONHOME
export HERMES_HOME="$HERMES_HOME"
export PLAYWRIGHT_BROWSERS_PATH="$PW_HOME"
export PATH="$HERMES_HOME/bin:$NODE_DIR/bin:\$PATH"
exec "$VENV/bin/python" "$entry" "\$@"
EOF
    chmod 0755 "$p"
    log_ok "$name → $p"
}

write_shim hermes      "$INSTALL_DIR/hermes"
write_shim hermes-agent "$INSTALL_DIR/run_agent.py"
write_shim hermes-acp  "$INSTALL_DIR/hermes"

# .env 里也写一份环境变量，供 `hermes` 内部 spawn 的子进程继承
ENV_FILE="$HERMES_HOME/.env"
{
    echo ""
    echo "# ── 离线安装自动写入 ──"
    grep -q '^PLAYWRIGHT_BROWSERS_PATH=' "$ENV_FILE" 2>/dev/null || echo "PLAYWRIGHT_BROWSERS_PATH=$PW_HOME"
} >> "$ENV_FILE" 2>/dev/null || true

# =============================================================================
# 完成
# =============================================================================
echo ""
echo -e "${C_GREEN}${C_BOLD}════════════════════════════════════════════════════════${C_OFF}"
echo -e "${C_GREEN}${C_BOLD}  ✅ Hermes Agent 离线安装完成${C_OFF}"
echo -e "${C_GREEN}${C_BOLD}════════════════════════════════════════════════════════${C_OFF}"
echo ""
echo "  代码目录 : $INSTALL_DIR"
echo "  数据目录 : $HERMES_HOME"
echo "  命令入口 : $LINK_DIR/hermes"
echo "  Python   : $("$VENV/bin/python" --version 2>&1)（自带，未使用系统 Python）"
if [ -f "$WEB_DIST/index.html" ]; then
    echo "  Web 控制台: hermes dashboard  →  http://127.0.0.1:9119"
else
    echo "  Web 控制台: 未预编译（dashboard 需现场构建，离线环境不可用）"
fi
echo ""
echo -e "${C_BOLD}接下来：${C_OFF}"
echo "  1) 配置模型（必做）—— Hermes 只是个壳，必须接一个模型才能干活："
echo "       vi $HERMES_HOME/.env"
echo "     或直接： hermes setup"
echo "  2) 自检：  hermes doctor"
echo "  3) 命令行： hermes"
echo "  4) 图形界面： hermes dashboard      （浏览器打开 http://127.0.0.1:9119）"
echo ""
# 这里刻意不用 `echo "$PATH" | tr ':' '\n' | grep -qx "$LINK_DIR"`：
# grep -q 命中即退出，会让上游 tr 吃 EPIPE；在 set -o pipefail 下整条管道
# 判为非零，再被 `!` 反转 —— 于是"PATH 里明明已经有了"也会被误报成"不在
# PATH 上"，让用户重复往 rc 文件里追加 export。改用纯 bash 的 case 做
# 子串匹配：无管道、无竞态、语义精确。
case ":$PATH:" in
    *":$LINK_DIR:"*)
        : # 已在 PATH 上，无需提示
        ;;
    *)
        log_warn "$LINK_DIR 不在当前 PATH 上"
        echo "     当前会话临时生效： export PATH=\"$LINK_DIR:\$PATH\""
        echo "     永久生效（bash）： echo 'export PATH=\"$LINK_DIR:\$PATH\"' >> ~/.bashrc"
        ;;
esac
echo -e "${C_YELLOW}注意${C_OFF}：离线环境下 \`hermes update\` 与任何自动装包（lazy-install）都不可用，"
echo "      这是预期行为 —— 需要升级时重新构建离线包并在本机重跑 install.sh。"
echo ""
