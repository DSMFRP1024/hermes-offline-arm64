#!/usr/bin/env bash
# =============================================================================
# Hermes Agent 离线安装 —— 只读环境预检
# =============================================================================
#
# 这个脚本**不改任何东西**：只跑只读命令，把会和安装/运行冲突的地方提前摊开。
# 装之前跑一次，能把"装到一半才发现 glibc 不对 / 磁盘不够 / Chromium 缺库"
# 这类返工挡在前面。
#
# 用法：
#     bash check-env.sh
#
# 退出码：0 = 无明显阻塞项；1 = 有阻塞项（install.sh 大概率会失败或降级）。
# =============================================================================

set -uo pipefail

BUNDLE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ -t 1 ]; then
    C_RED='\033[0;31m'; C_GREEN='\033[0;32m'; C_YELLOW='\033[0;33m'
    C_CYAN='\033[0;36m'; C_BOLD='\033[1m'; C_OFF='\033[0m'
else
    C_RED=''; C_GREEN=''; C_YELLOW=''; C_CYAN=''; C_BOLD=''; C_OFF=''
fi

ok()   { echo -e "  ${C_GREEN}✓${C_OFF} $*"; }
warn() { echo -e "  ${C_YELLOW}⚠${C_OFF} $*"; WARNS=$((WARNS + 1)); }
bad()  { echo -e "  ${C_RED}✗${C_OFF} $*"; FAILS=$((FAILS + 1)); }
head_() { echo ""; echo -e "${C_BOLD}$*${C_OFF}"; }

WARNS=0
FAILS=0
GLIBC_MINOR_REQUIRED=28

echo ""
echo -e "${C_BOLD}════════════════════════════════════════════════════════${C_OFF}"
echo -e "${C_BOLD}  Hermes Agent 离线安装 —— 环境预检（只读）${C_OFF}"
echo -e "${C_BOLD}════════════════════════════════════════════════════════${C_OFF}"

# -----------------------------------------------------------------------------
head_ "1. 系统与架构"
ARCH="$(uname -m)"
case "$ARCH" in
    aarch64|arm64) ok "架构 $ARCH" ;;
    *) bad "架构 $ARCH —— 本包只支持 aarch64/arm64" ;;
esac

if [ -r /etc/os-release ]; then
    # shellcheck disable=SC1091
    OS_NAME="$(. /etc/os-release && echo "${PRETTY_NAME:-${ID:-unknown}}")"
    ok "发行版 $OS_NAME"
else
    warn "读不到 /etc/os-release，无法识别发行版"
fi

KERNEL="$(uname -r)"
ok "内核 $KERNEL"

# -----------------------------------------------------------------------------
head_ "2. glibc 版本（决定 wheel 能否加载）"
if command -v getconf >/dev/null 2>&1; then
    RAW="$(getconf GNU_LIBC_VERSION 2>/dev/null || true)"
    if [ -n "$RAW" ]; then
        MAJ="$(printf '%s' "$RAW" | sed -n 's/.* \([0-9]*\)\.\([0-9]*\).*/\1/p')"
        MIN="$(printf '%s' "$RAW" | sed -n 's/.* \([0-9]*\)\.\([0-9]*\).*/\2/p')"
        if [ "$MAJ" -gt 2 ] || { [ "$MAJ" -eq 2 ] && [ "$MIN" -ge "$GLIBC_MINOR_REQUIRED" ]; }; then
            ok "$RAW（>= 基线 2.$GLIBC_MINOR_REQUIRED）"
        else
            bad "$RAW 低于基线 2.$GLIBC_MINOR_REQUIRED —— 原生扩展会 GLIBC_x.y not found"
            echo "      需要重打：build_bundle.py --glibc 2.17"
        fi
    else
        warn "getconf 没有返回 GNU_LIBC_VERSION"
    fi
else
    warn "没有 getconf，无法检测 glibc"
fi

# -----------------------------------------------------------------------------
head_ "3. 目标目录与磁盘空间"
INSTALL_DIR="/usr/local/lib/hermes-agent"
[ "$(id -u)" -eq 0 ] || INSTALL_DIR="$HOME/.hermes/hermes-agent"
HERMES_HOME="$HOME/.hermes"
[ "$(id -u)" -eq 0 ] && HERMES_HOME="/root/.hermes"

ok "当前用户 uid=$(id -u) → 安装目录 $INSTALL_DIR"

BUNDLE_MB="$(du -sm "$BUNDLE_DIR" 2>/dev/null | awk '{print $1}' || echo 0)"
NEED_MB=$(( BUNDLE_MB * 3 + 500 ))
echo "      离线包本体 ${BUNDLE_MB}MB，预计需要 ${NEED_MB}MB 可用空间"

check_fs() {
    local path="$1" label="$2"
    local probe="$path"
    while [ ! -d "$probe" ] && [ "$probe" != "/" ]; do probe="$(dirname "$probe")"; done
    local avail
    avail="$(df -Pk "$probe" 2>/dev/null | awk 'NR==2{print int($4/1024)}')"
    if [ -z "$avail" ]; then
        warn "$label：无法检测 $probe 的可用空间"
    elif [ "$avail" -lt "$NEED_MB" ]; then
        bad "$label：$probe 仅剩 ${avail}MB < 需要 ${NEED_MB}MB"
    else
        ok "$label：$probe 可用 ${avail}MB"
    fi
}

check_fs "$INSTALL_DIR" "安装目录"
[ "$INSTALL_DIR" != "$HERMES_HOME" ] && check_fs "$HERMES_HOME" "数据目录"

# -----------------------------------------------------------------------------
head_ "4. 包完整性"
if [ -f "$BUNDLE_DIR/MANIFEST.sha256" ]; then
    MISSING="$(cd "$BUNDLE_DIR" && sha256sum -c MANIFEST.sha256 2>/dev/null | grep -v ': OK$' || true)"
    if [ -z "$MISSING" ]; then
        ok "MANIFEST.sha256 全部通过（$(wc -l < "$BUNDLE_DIR/MANIFEST.sha256") 个文件）"
    else
        bad "有文件校验不通过："
        printf '%s\n' "$MISSING" | head -10 | sed 's/^/      /'
    fi
else
    warn "没有 MANIFEST.sha256，无法校验完整性"
fi

for f in requirements.lock.txt repo/hermes-agent-src.tar.gz runtime wheels; do
    if [ -e "$BUNDLE_DIR/$f" ]; then ok "存在 $f"; else bad "缺少 $f"; fi
done

N_WHEELS="$(ls "$BUNDLE_DIR"/wheels/*.whl 2>/dev/null | wc -l)"
N_PREBUILT="$(ls "$BUNDLE_DIR"/wheels-prebuilt/*.whl 2>/dev/null | wc -l)"
ok "wheel：$N_WHEELS 个（另有 $N_PREBUILT 个本地预构建）"

# -----------------------------------------------------------------------------
head_ "5. 包内容概览"
PY_TGZ="$(ls "$BUNDLE_DIR"/runtime/cpython-*.tar.gz 2>/dev/null | head -1 || true)"
[ -n "$PY_TGZ" ] && ok "Python 运行时 $(basename "$PY_TGZ")" || bad "缺少 Python 运行时"

NODE_TGZ="$(ls "$BUNDLE_DIR"/runtime/node-v*-linux-arm64.tar.xz 2>/dev/null | head -1 || true)"
[ -n "$NODE_TGZ" ] && ok "Node.js $(basename "$NODE_TGZ")" || warn "没有 Node（浏览器工具不可用）"

N_NM="$(ls "$BUNDLE_DIR"/node_modules/*.tar.gz 2>/dev/null | wc -l)"
[ "$N_NM" -gt 0 ] && ok "预构建 node_modules：$N_NM 个 tarball" || warn "没有预构建 node_modules"

[ -d "$BUNDLE_DIR/browsers" ] && [ -n "$(ls -A "$BUNDLE_DIR/browsers" 2>/dev/null)" ] \
    && ok "Playwright 浏览器缓存 $(du -sm "$BUNDLE_DIR/browsers" | awk '{print $1}')MB" \
    || warn "没有浏览器缓存（浏览器工具不可用）"

for b in rg ffmpeg; do
    [ -f "$BUNDLE_DIR/bin/$b" ] && ok "附带 $b" || warn "没有 $b"
done
ls "$BUNDLE_DIR"/lib/*.so >/dev/null 2>&1 && ok "附带 fts5_cjk 原生扩展" || warn "没有 fts5_cjk（中文检索会变慢）"

# dashboard 前端。离线机上没有 npm registry，dist 必须由包自带：
# 否则 `hermes dashboard` 会在目标机现场 npm install → 失败 → exit 1。
# 前端藏在嵌套的源码包里，要钻进去查；只读，用完删掉清单文件。
WEB_LIST="$(mktemp 2>/dev/null || echo "/tmp/hermes-weblist-$$")"
if tar -tzf "$BUNDLE_DIR/repo/hermes-agent-src.tar.gz" > "$WEB_LIST" 2>/dev/null; then
    if grep -qxF 'hermes-agent/hermes_cli/web_dist/index.html' "$WEB_LIST"; then
        N_WEB="$(grep -cE 'web_dist/assets/.*\.(js|css)$' "$WEB_LIST" || true)"
        ok "dashboard 前端已预编译（web_dist/assets 有 ${N_WEB:-0} 个 js/css，离线免 npm）"
    else
        warn "包里没有预编译的 dashboard 前端 —— 离线环境下 Web 控制台起不来"
        echo "      需要重打：构建时不要加 --skip-web-ui"
    fi
else
    warn "读不出 repo/hermes-agent-src.tar.gz，无法确认 dashboard 前端"
fi
rm -f "$WEB_LIST"

if [ -f "$BUNDLE_DIR/build-info.json" ]; then
    echo ""
    echo "  ── 构建信息 ──"
    sed 's/^/      /' "$BUNDLE_DIR/build-info.json"
fi

# -----------------------------------------------------------------------------
head_ "6. Chromium 运行时依赖（信创机上最容易缺的一环）"
if [ -d "$BUNDLE_DIR/browsers" ]; then
    # 浏览器是 tar.gz，先在临时目录里抽一个 chrome 出来 ldd —— 只读检查，
    # 不动目标路径，用完删除。
    TMP="$(mktemp -d 2>/dev/null || echo /tmp/hermes-check-$$)"
    mkdir -p "$TMP"
    PW_ARC="$(ls "$BUNDLE_DIR"/browsers/*.tar.gz 2>/dev/null | head -1 || true)"
    if [ -n "$PW_ARC" ] && tar -xzf "$PW_ARC" -C "$TMP" 2>/dev/null; then
        CHROME_BIN="$(ls -d "$TMP"/chromium-*/chrome-linux*/chrome 2>/dev/null | head -1 || true)"
        if [ -n "$CHROME_BIN" ]; then
            MISSING="$(ldd "$CHROME_BIN" 2>/dev/null | awk '/not found/{print $1}' | sort -u || true)"
            if [ -z "$MISSING" ]; then
                ok "Chromium 动态库完整"
            else
                warn "Chromium 缺少以下系统库（浏览器工具会启动失败）："
                printf '%s\n' "$MISSING" | sed 's/^/      /'
                echo "      CentOS/openEuler/Kylin 系："
                echo "        sudo yum install -y nss nspr libdrm libxkbcommon mesa-libgbm pango cairo alsa-lib atk at-spi2-core cups-libs"
                echo "      Debian/UOS 系："
                echo "        sudo apt install -y libnss3 libnspr4 libdrm2 libxkbcommon0 libgbm1 libpango-1.0-0 libcairo2 libasound2 libatk1.0-0 libatk-bridge2.0-0 libcups2"
            fi
        else
            warn "浏览器缓存里没找到 chrome 可执行文件"
        fi
    else
        warn "浏览器缓存解压失败"
    fi
    rm -rf "$TMP"
else
    warn "包内没有浏览器缓存，跳过"
fi

# -----------------------------------------------------------------------------
head_ "7. 局域网内可达性（Telegram / webhook 等网关功能需要）"
if command -v ip >/dev/null 2>&1; then
    ip -brief addr show 2>/dev/null | grep -v '^lo' | sed 's/^/      /' || true
elif command -v ifconfig >/dev/null 2>&1; then
    ifconfig 2>/dev/null | grep -E 'inet ' | sed 's/^/      /' || true
else
    warn "没有 ip/ifconfig，无法列出网卡"
fi

# -----------------------------------------------------------------------------
echo ""
echo -e "${C_BOLD}════════════════════════════════════════════════════════${C_OFF}"
if [ "$FAILS" -gt 0 ]; then
    echo -e "${C_RED}${C_BOLD}  预检结果：$FAILS 个阻塞项，$WARNS 个警告${C_OFF}"
    echo "  阻塞项会让安装失败或严重降级，请先处理。"
    echo -e "${C_BOLD}════════════════════════════════════════════════════════${C_OFF}"
    exit 1
elif [ "$WARNS" -gt 0 ]; then
    echo -e "${C_YELLOW}${C_BOLD}  预检结果：0 个阻塞项，$WARNS 个警告${C_OFF}"
    echo "  警告项只影响对应功能，核心 CLI 仍可正常安装使用。"
    echo -e "${C_BOLD}════════════════════════════════════════════════════════${C_OFF}"
    exit 0
else
    echo -e "${C_GREEN}${C_BOLD}  ✅ 预检全部通过${C_OFF}"
    echo -e "${C_BOLD}════════════════════════════════════════════════════════${C_OFF}"
    exit 0
fi
