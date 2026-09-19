#!/usr/bin/env bash
# =============================================================================
# Hermes 离线增强包 —— 安装脚本（ARM64 / 全程离线）
# =============================================================================
#
# 装什么：
#   1. Python 包  → 装进 **hermes 自己的 venv**（office / PDF / 图像 / 视频 / 数据 / 编程）
#   2. MCP 服务器 → Python 侧装进**独立 venv**（$INSTALL_DIR/mcp/venv），
#                   Node 侧用预装的 node_modules（$INSTALL_DIR/mcp/node）
#   3. Skill      → 从本地 optional-skills/ 启用一批官方可选技能（零下载）
#
# 设计要点（改之前先读）：
#   1. 为什么 MCP 要独立 venv：hermes 自带 mcp==2.0.0，而 fastmcp 2.x 要求
#      mcp<2 —— 装在同一个 venv 里必然把 hermes 的原生 MCP 客户端顶掉。
#      独立之后各自收敛，互不干扰。
#   2. 为什么先存一份 pip freeze：extras 与 hermes 共用一个 venv，理论上存在
#      "extras 把 hermes 的某个依赖顶掉"的风险。装完立刻跑 hermes 冒烟测试 +
#      对比 freeze 差异，出问题会当场指出来，而不是让你几天后才发现控制台打不开。
#   3. 全程 pip --no-index：离线机上任何索引访问都会以**超时**形式挂住，
#      而不是快速失败，所以把 --no-index 写死在封装函数里。
#   4. 幂等：重复执行只补缺的；config.yaml 先备份再改，只动 mcp_servers 这一节，
#      且**绝不覆盖你自己写的同名条目**。
#
# 用法：
#     bash install-extras.sh [选项]
#
#     --install-dir PATH   hermes 安装目录（默认自动探测）
#     --hermes-home PATH   数据目录（默认自动探测）
#     --fs-root DIR        filesystem MCP 允许访问的目录，可重复；默认取存在的
#                          ~/Documents、~/Desktop、~/Downloads，再加 $HERMES_HOME/workspace
#     --force              重装 Python 包与 MCP venv
#     --skip-python        不装 Python 包
#     --skip-mcp           不装 MCP 服务器
#     --skip-skills        不启用 optional skill
#     --no-shims           不往 $HERMES_HOME/bin 放 python3/pip 垫片
#     --no-keep-wheels     不把 wheelhouse 留在安装目录（省几百 MB）
#     --dry-run            只打印将要做什么
#     --no-verify          跳过装完后的验证
#     -h, --help           显示本说明
#
# 装完后 `hermes` 直接可用；MCP 工具名形如 mcp_office_pptx_* / mcp_sqlite_*。
# =============================================================================

set -euo pipefail

C_OFF=$'\033[0m'; C_BOLD=$'\033[1m'; C_RED=$'\033[31m'
C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'; C_BLUE=$'\033[34m'

log_ok()   { echo -e "${C_GREEN}✓${C_OFF} $*"; }
log_info() { echo -e "${C_BLUE}·${C_OFF} $*"; }
log_warn() { echo -e "${C_YELLOW}!${C_OFF} $*"; }
log_err()  { echo -e "${C_RED}✗${C_OFF} $*" >&2; }
log_step() { echo ""; echo -e "${C_BOLD}── $* ──${C_OFF}"; }
die()      { log_err "$*"; exit 1; }

# ── 默认值 ──
FORCE=false
SKIP_PYTHON=false
SKIP_MCP=false
SKIP_SKILLS=false
SHIMS=true
KEEP_WHEELS=true
DRY_RUN=false
VERIFY=true
INSTALL_DIR=""
HERMES_HOME="${HERMES_HOME:-}"
FS_ROOTS=()

usage() { sed -n '3,43p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0; }

while [ $# -gt 0 ]; do
    case "$1" in
        --install-dir)    INSTALL_DIR="$2"; shift 2 ;;
        --hermes-home)    HERMES_HOME="$2"; shift 2 ;;
        --fs-root)        FS_ROOTS+=("$2"); shift 2 ;;
        --force)          FORCE=true; shift ;;
        --skip-python)    SKIP_PYTHON=true; shift ;;
        --skip-mcp)       SKIP_MCP=true; shift ;;
        --skip-skills)    SKIP_SKILLS=true; shift ;;
        --no-shims)       SHIMS=false; shift ;;
        --no-keep-wheels) KEEP_WHEELS=false; shift ;;
        --dry-run)        DRY_RUN=true; shift ;;
        --no-verify)      VERIFY=false; shift ;;
        -h|--help)        usage ;;
        *) die "未知参数: $1（用 --help 看用法）" ;;
    esac
done

BUNDLE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IS_ROOT=false
[ "$(id -u)" -eq 0 ] && IS_ROOT=true

echo ""
echo -e "${C_BOLD}┌─────────────────────────────────────────────────────┐${C_OFF}"
echo -e "${C_BOLD}│   ☤ Hermes 离线增强包（办公 / 图像 / 视频 / MCP）    │${C_OFF}"
echo -e "${C_BOLD}└─────────────────────────────────────────────────────┘${C_OFF}"
echo ""

# =============================================================================
# 0. 定位已有的 hermes 安装
# =============================================================================
log_step "0. 定位 hermes 安装"

if [ -f "$BUNDLE_DIR/install.sh" ]; then
    die "这看起来是**主包**的目录。请把增强包解压到别处再运行本脚本。"
fi

STAMP_FILE=""
for cand in "${HERMES_HOME:-}" "$HOME/.hermes" "/root/.hermes"; do
    if [ -n "${cand:-}" ] && [ -f "$cand/.offline-install" ]; then
        STAMP_FILE="$cand/.offline-install"; break
    fi
done

if [ -z "$INSTALL_DIR" ] && [ -n "$STAMP_FILE" ]; then
    INSTALL_DIR="$(sed -n 's/^install_dir=//p' "$STAMP_FILE" | sed -n '1p' || true)"
fi
if [ -z "$HERMES_HOME" ] && [ -n "$STAMP_FILE" ]; then
    HERMES_HOME="$(sed -n 's/^hermes_home=//p' "$STAMP_FILE" | sed -n '1p' || true)"
fi

if [ -z "$INSTALL_DIR" ]; then
    for cand in /usr/local/lib/hermes-agent "$HOME/.hermes/hermes-agent"; do
        if [ -x "$cand/venv/bin/python" ]; then INSTALL_DIR="$cand"; break; fi
    done
fi
if [ -z "$HERMES_HOME" ]; then
    if [ "$IS_ROOT" = true ]; then HERMES_HOME="/root/.hermes"; else HERMES_HOME="$HOME/.hermes"; fi
fi

[ -n "$INSTALL_DIR" ] || die "找不到 hermes 安装目录，请用 --install-dir 指定"
VENV="$INSTALL_DIR/venv"
[ -x "$VENV/bin/python" ] || die "$VENV/bin/python 不存在 —— 先跑主包的 install.sh"
VPY="$VENV/bin/python"
PY_BIN="$INSTALL_DIR/runtime/python/bin/python3"
[ -x "$PY_BIN" ] || PY_BIN="$VPY"

MCP_DIR="$INSTALL_DIR/mcp"
MCP_VENV="$MCP_DIR/venv"
NODE_BIN="$HERMES_HOME/node/bin/node"

log_ok "安装目录 $INSTALL_DIR"
log_ok "数据目录 $HERMES_HOME"

# =============================================================================
# 1. 校验增强包自身
# =============================================================================
log_step "1. 校验增强包完整性"

ARCH="$(uname -m)"
case "$ARCH" in
    aarch64|arm64) log_ok "架构 $ARCH" ;;
    *) die "本包只支持 aarch64/arm64，当前是 $ARCH" ;;
esac

for f in install-extras.sh verify-mcp.py merge-mcp-config.py README.md \
         requirements-extras.lock.txt requirements-mcp.lock.txt mcp-servers.json; do
    [ -e "$BUNDLE_DIR/$f" ] || die "增强包不完整：缺少 $f"
done
[ -d "$BUNDLE_DIR/wheels" ] || die "增强包不完整：缺少 wheels/"
[ -d "$BUNDLE_DIR/mcp-wheels" ] || die "增强包不完整：缺少 mcp-wheels/"

if [ "$VERIFY" = true ] && [ -f "$BUNDLE_DIR/MANIFEST.sha256" ]; then
    if (cd "$BUNDLE_DIR" && sha256sum -c --quiet MANIFEST.sha256 2>/dev/null); then
        log_ok "MANIFEST.sha256 全部通过"
    else
        log_warn "MANIFEST.sha256 校验未通过（传输过程可能损坏）"
    fi
else
    log_info "跳过 MANIFEST 校验"
fi

N_EXTRA_WHEELS=$(ls -1 "$BUNDLE_DIR/wheels" 2>/dev/null | grep -c '\.whl$' || true)
N_MCP_WHEELS=$(ls -1 "$BUNDLE_DIR/mcp-wheels" 2>/dev/null | grep -c '\.whl$' || true)
log_ok "轮子 $N_EXTRA_WHEELS 个（hermes venv）+ $N_MCP_WHEELS 个（MCP venv）"

BUNDLE_MB="$(du -sm "$BUNDLE_DIR" 2>/dev/null | awk '{print $1}')"
BUNDLE_MB="${BUNDLE_MB:-0}"
NEED_MB=$(( BUNDLE_MB * 2 + 300 ))
TARGET_FS="$(df -Pk "$INSTALL_DIR" 2>/dev/null | awk 'NR==2{print $4}')"
TARGET_FS="${TARGET_FS:-0}"
TARGET_FS=$(( TARGET_FS / 1024 ))
if [ "$TARGET_FS" -gt 0 ] && [ "$TARGET_FS" -lt "$NEED_MB" ]; then
    die "$INSTALL_DIR 所在分区只剩 ${TARGET_FS}MB，估计需要 ${NEED_MB}MB"
fi
log_ok "磁盘空间 可用 ${TARGET_FS}MB / 预计需要 ${NEED_MB}MB"

if [ "$DRY_RUN" = true ]; then
    echo ""
    log_info "--dry-run：到此为止。将要执行："
    [ "$SKIP_PYTHON" = false ] && log_info "  · 把 $N_EXTRA_WHEELS 个轮子装进 $VENV"
    [ "$SKIP_MCP" = false ] && log_info "  · 建 $MCP_VENV 并装入 MCP 服务器"
    [ "$SKIP_SKILLS" = false ] && log_info "  · 按 skills-official.txt 启用官方可选技能"
    exit 0
fi

# =============================================================================
# 2. Python 包（装进 hermes 自己的 venv）
# =============================================================================
log_step "2. 安装 Python 包"

WHEELHOUSE="$INSTALL_DIR/wheelhouse-extras"
FREEZE_PRE="$VENV/.extras-prev-freeze.txt"
FREEZE_POST="$VENV/.extras-post-freeze.txt"

if [ "$KEEP_WHEELS" = true ]; then
    mkdir -p "$WHEELHOUSE"
    cp -f "$BUNDLE_DIR"/wheels/*.whl "$WHEELHOUSE/" 2>/dev/null || true
    cp -f "$BUNDLE_DIR"/mcp-wheels/*.whl "$WHEELHOUSE/" 2>/dev/null || true
    log_ok "wheelhouse 已落到 $WHEELHOUSE（离线 pip install 可直接取）"
fi

pip_offline() {
    env PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_INPUT=1 PIP_NO_INDEX=1 \
        "$VPY" -m pip "$@" --no-index \
        --find-links "$BUNDLE_DIR/wheels" --find-links "$BUNDLE_DIR/mcp-wheels" \
        --find-links "$WHEELHOUSE"
}

if [ "$SKIP_PYTHON" = true ]; then
    log_info "按要求跳过"
elif [ -f "$FREEZE_PRE" ] && [ "$FORCE" != true ]; then
    log_ok "之前已装过（$FREEZE_PRE 存在），跳过 —— 用 --force 重装"
else
    "$VPY" -m pip freeze > "$FREEZE_PRE" 2>/dev/null || true
    log_info "已记录安装前的依赖快照（用于事后对比）"

    N_PKGS=$(grep -c -v '^[[:space:]]*#' "$BUNDLE_DIR/requirements-extras.lock.txt" || true)
    log_info "安装 ${N_PKGS:-0} 个包（全程 --no-index）..."
    # 刻意不加 --force-reinstall：pip 默认 only-if-needed，已经装好且满足约束的包
    # 不会被动。这样 extras 才不会顺手把 hermes 的依赖顶掉。
    pip_offline install -r "$BUNDLE_DIR/requirements-extras.lock.txt" --no-build-isolation \
        || die "Python 包安装失败。先跑主包的 bash check-env.sh 看 glibc / 系统库"
    log_ok "Python 包安装完成"

    "$VPY" -m pip freeze > "$FREEZE_POST" 2>/dev/null || true

    # 原生扩展在 glibc 不匹配时只会在 import 期才炸，所以这里逐个 import 一遍。
    "$VPY" - <<'PYEOF' || die "增强包关键依赖 import 失败，包可能与本机 glibc 不匹配"
import sys
mods = ["docx", "openpyxl", "pptx", "pypdf", "reportlab", "fitz", "PIL",
        "numpy", "pandas", "matplotlib", "imageio", "jinja2"]
bad = []
for m in mods:
    try:
        __import__(m)
    except Exception as e:  # noqa: BLE001
        bad.append(f"{m}: {type(e).__name__}: {e}")
if bad:
    print("以下增强包依赖导入失败：")
    for b in bad:
        print("  -", b)
    sys.exit(1)
print(f"  ✓ {len(mods)} 个增强包关键依赖 import 正常")
PYEOF
fi

# =============================================================================
# 3. MCP 服务器（Python 侧，独立 venv）
# =============================================================================
log_step "3. 安装 MCP 服务器（Python 侧）"

if [ "$SKIP_MCP" = true ]; then
    log_info "按要求跳过"
else
    if [ -x "$MCP_VENV/bin/python" ] && [ "$FORCE" != true ]; then
        log_ok "MCP venv 已存在，复用（--force 可重建）"
    else
        if [ -d "$MCP_VENV" ]; then
            log_info "--force：删除已有 MCP venv"
            rm -rf "$MCP_VENV"
        fi
        mkdir -p "$MCP_DIR"
        log_info "创建 MCP venv（独立的，不碰 hermes 的 venv）..."
        "$PY_BIN" -m venv "$MCP_VENV" || die "MCP venv 创建失败"
    fi
    [ -x "$MCP_VENV/bin/python" ] || die "$MCP_VENV/bin/python 不存在"

    N_MCP_PKGS=$(grep -c -v '^[[:space:]]*#' "$BUNDLE_DIR/requirements-mcp.lock.txt" || true)
    log_info "安装 ${N_MCP_PKGS:-0} 个 MCP 依赖..."
    env PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_INPUT=1 PIP_NO_INDEX=1 \
        "$MCP_VENV/bin/python" -m pip install \
        -r "$BUNDLE_DIR/requirements-mcp.lock.txt" \
        --no-index --find-links "$BUNDLE_DIR/mcp-wheels" --no-build-isolation \
        || die "MCP 依赖安装失败"
    log_ok "MCP 依赖安装完成"

    MCP_VER="$("$MCP_VENV/bin/python" -c 'import mcp; print(mcp.__version__)' 2>/dev/null || echo '?')"
    HERMES_MCP_VER="$("$VPY" -c 'import mcp; print(mcp.__version__)' 2>/dev/null || echo '?')"
    if [ "$MCP_VER" = "$HERMES_MCP_VER" ] && [ "$MCP_VER" != "?" ]; then
        log_warn "两边 mcp 版本都是 $MCP_VER —— 独立 venv 没起作用，检查一下"
    else
        log_ok "MCP venv 用 mcp==$MCP_VER；hermes venv 仍用 mcp==$HERMES_MCP_VER（互不影响）"
    fi
fi

# =============================================================================
# 4. MCP 服务器（Node 侧）与统一清单
# =============================================================================
log_step "4. 部署 MCP 服务器（Node 侧）并生成清单"

if [ "$SKIP_MCP" = false ]; then
    if [ -d "$BUNDLE_DIR/mcp-node/node_modules" ]; then
        rm -rf "$MCP_DIR/node"
        mkdir -p "$MCP_DIR/node"
        cp -a "$BUNDLE_DIR/mcp-node/." "$MCP_DIR/node/"
        log_ok "Node MCP 服务器已部署到 $MCP_DIR/node"
    else
        log_warn "增强包里没有 mcp-node/，Node 侧服务器将缺席"
    fi
fi

# 允许访问的目录：显式 --fs-root 优先，否则取存在的 ~/Documents、~/Desktop、~/Downloads
mkdir -p "$HERMES_HOME/workspace"
FS_LIST=()
if [ "${#FS_ROOTS[@]}" -gt 0 ]; then
    FS_LIST=("${FS_ROOTS[@]}")
else
    for d in "$HOME/Documents" "$HOME/Desktop" "$HOME/Downloads" "$HOME/文档"; do
        [ -d "$d" ] && FS_LIST+=("$d")
    done
fi
FS_LIST+=("$HERMES_HOME/workspace")

if [ "$SKIP_MCP" = false ]; then
    EX_MCP_DIR="$MCP_DIR" EX_MCP_VENV="$MCP_VENV" EX_NODE_BIN="$NODE_BIN" \
    EX_MEMORY_FILE="$HERMES_HOME/mcp/memory.json" EX_MANIFEST="$BUNDLE_DIR/mcp-servers.json" \
    EX_SERVERS_JSON="$MCP_DIR/servers.json" \
    "$VPY" - "${FS_LIST[@]}" <<'PYEOF' || die "生成 MCP 清单失败"
import json, os, sys
from pathlib import Path

mcp_dir = Path(os.environ["EX_MCP_DIR"])
venv = Path(os.environ["EX_MCP_VENV"])
node_bin = Path(os.environ["EX_NODE_BIN"])
memory_file = os.environ["EX_MEMORY_FILE"]
fs_roots = sys.argv[1:]

manifest = json.loads(Path(os.environ["EX_MANIFEST"]).read_text(encoding="utf-8"))

servers, missing = [], []
for e in manifest.get("python", []):
    cmd = venv / "bin" / e["console"]
    if not cmd.exists():
        missing.append(f"{e['name']}（缺 {cmd.name}）")
        continue
    servers.append({"name": e["name"], "kind": "python", "label": e.get("label", ""),
                    "package": e["package"], "command": str(cmd), "args": [],
                    "env": {}, "timeout": int(e.get("timeout", 120))})

node_root = mcp_dir / "node"
for e in manifest.get("node", []):
    entry = node_root / e.get("entry", "")
    if not (node_bin.exists() and entry.is_file()):
        missing.append(f"{e['name']}（缺 node 或入口）")
        continue
    mode = e.get("arg_mode", "none")
    if mode == "fs_roots":
        args = [str(entry), *fs_roots]
    elif mode == "memory_path":
        Path(memory_file).parent.mkdir(parents=True, exist_ok=True)
        args = [str(entry), memory_file]
    else:
        args = [str(entry)]
    env = {k: v.replace("@@MEMORY_FILE@@", memory_file)
           for k, v in (e.get("env") or {}).items()}
    servers.append({"name": e["name"], "kind": "node", "label": e.get("label", ""),
                    "package": e["package"], "command": str(node_bin), "args": args,
                    "env": env, "timeout": int(e.get("timeout", 120))})

out = Path(os.environ["EX_SERVERS_JSON"])
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps(servers, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(f"  ✓ {len(servers)} 个 MCP 服务器写入 {out}")
for s in servers:
    extra = " ".join(s["args"][1:]) if s["kind"] == "node" else ""
    print(f"      {s['name']:18s} [{s['kind']:6s}] {extra}")
for m in missing:
    print(f"  ! 缺席：{m}")
PYEOF
fi

# =============================================================================
# 5. 写进 config.yaml
# =============================================================================
log_step "5. 写入 mcp_servers 配置"

CFG="$HERMES_HOME/config.yaml"
TEMPLATE="$INSTALL_DIR/cli-config.yaml.example"

if [ "$SKIP_MCP" = true ]; then
    log_info "按要求跳过"
elif [ ! -f "$MCP_DIR/servers.json" ]; then
    log_warn "没有 servers.json，跳过配置写入"
else
    "$VPY" "$BUNDLE_DIR/merge-mcp-config.py" \
        --config "$CFG" --servers "$MCP_DIR/servers.json" \
        --template "$TEMPLATE" --mcp-dir "$MCP_DIR" \
        || die "写入 config.yaml 失败"
fi

# =============================================================================
# 6. 启用官方 optional skill
# =============================================================================
log_step "6. 启用官方可选技能"

SKILLS_SRC="$INSTALL_DIR/optional-skills"
if [ "$SKIP_SKILLS" = true ]; then
    log_info "按要求跳过"
elif [ ! -d "$SKILLS_SRC" ]; then
    log_warn "没有 $SKILLS_SRC，跳过"
elif [ ! -f "$BUNDLE_DIR/skills-official.txt" ]; then
    log_warn "增强包里没有 skills-official.txt，跳过"
else
    HAVE_TIMEOUT=false
    command -v timeout >/dev/null 2>&1 && HAVE_TIMEOUT=true

    n_ok=0; n_copy=0; n_skip=0; n_fail=0
    while IFS= read -r line; do
        rel="${line%%#*}"
        rel="$(printf '%s' "$rel" | tr -d '[:space:]')"
        [ -n "$rel" ] || continue
        src="$SKILLS_SRC/$rel"
        dest="$HERMES_HOME/skills/$rel"
        if [ ! -d "$src" ]; then
            log_warn "  $rel：本地 optional-skills 里没有，跳过"
            n_fail=$((n_fail + 1)); continue
        fi
        if [ -d "$dest" ]; then
            n_skip=$((n_skip + 1)); continue
        fi

        # 首选官方 CLI：它会把来源登记进 hub lock，日后 `hermes skills list` 才认得出。
        # OptionalSkillSource 是「本地优先」的，本地命中就不会碰网络；但为了保险，
        # 还是套一层 timeout —— 万一它去 GitHub 兜底，离线机上会挂住而不是快速失败。
        installed=false
        if [ "$HAVE_TIMEOUT" = true ]; then
            if timeout 90 "$VPY" "$INSTALL_DIR/hermes" skills install "official/$rel" --yes \
                    >/dev/null 2>&1; then installed=true; fi
        else
            if "$VPY" "$INSTALL_DIR/hermes" skills install "official/$rel" --yes \
                    >/dev/null 2>&1; then installed=true; fi
        fi

        if [ "$installed" = true ]; then
            n_ok=$((n_ok + 1))
        else
            # 兜底：直接拷贝。install_path 就是 optional-skills 下的相对路径。
            mkdir -p "$(dirname "$dest")"
            if cp -a "$src" "$dest" 2>/dev/null; then
                n_copy=$((n_copy + 1))
            else
                log_warn "  $rel：安装失败"
                n_fail=$((n_fail + 1))
            fi
        fi
    done < "$BUNDLE_DIR/skills-official.txt"

    log_ok "技能：CLI 安装 $n_ok 个 / 直接拷贝 $n_copy 个 / 已存在 $n_skip 个 / 失败 $n_fail 个"
    if [ "$n_skip" -gt 0 ]; then
        log_info "已存在的不动 —— 你自己改过的版本不会被覆盖"
    fi
    log_info "看装了哪些：$VPY $INSTALL_DIR/hermes skills list"
fi

# =============================================================================
# 7. python3 / pip 垫片
# =============================================================================
log_step "7. 放置 python3 / pip 垫片"

# 为什么需要：skill 里的脚本是按 `python3 xxx.py` 写的，而 hermes 的 venv 不在
# PATH 上（hermes 的入口脚本只把 $HERMES_HOME/bin 和 node 加进 PATH）。
# 不垫这一层，装了轮子 skill 脚本照样 import 不到 —— 而且是静默失败。
if [ "$SHIMS" = false ]; then
    log_info "按要求跳过（--no-shims）"
else
    mkdir -p "$HERMES_HOME/bin"
    for name in python3 python; do
        rm -f "$HERMES_HOME/bin/$name"
        printf '#!/usr/bin/env bash\n# Hermes 离线增强包生成 —— 指向 hermes 自带 venv\nexec "%s/bin/python" "$@"\n' \
            "$VENV" > "$HERMES_HOME/bin/$name"
        chmod 0755 "$HERMES_HOME/bin/$name"
    done

    # pip 垫片默认走离线：直连本地 wheelhouse，不碰索引。
    # 想联网装东西：export HERMES_PIP_ONLINE=1
    rm -f "$HERMES_HOME/bin/pip" "$HERMES_HOME/bin/pip3"
    for name in pip pip3; do
        cat > "$HERMES_HOME/bin/$name" <<EOF
#!/usr/bin/env bash
# Hermes 离线增强包生成。
# 默认 --no-index + 本地 wheelhouse（离线机上访问索引会以超时形式挂住）。
# 需要联网装包时：export HERMES_PIP_ONLINE=1
set -euo pipefail
EXTRA=()
if [ "\${HERMES_PIP_ONLINE:-}" != "1" ]; then
    EXTRA+=(--no-index)
    [ -d "$WHEELHOUSE" ] && EXTRA+=(--find-links "$WHEELHOUSE")
fi
exec "$VENV/bin/python" -m pip "\$@" "\${EXTRA[@]}"
EOF
        chmod 0755 "$HERMES_HOME/bin/$name"
    done
    log_ok "$HERMES_HOME/bin/{python3,python,pip,pip3} 已就位"
    log_info "（这几个垫片只在 hermes 启动的会话里生效，不会影响系统 PATH）"
fi

# =============================================================================
# 8. 验证
# =============================================================================
log_step "8. 验证"

HERMES_OK=true
if [ "$VERIFY" = false ]; then
    log_info "按要求跳过"
else
    # ── 8.1 hermes 本体还能不能起来 ──
    if "$VPY" -c "import hermes_cli, openai, cryptography, pydantic_core, PIL, psutil, yaml, httpx" \
            >/dev/null 2>&1; then
        log_ok "hermes 核心依赖 import 正常"
    else
        HERMES_OK=false
        log_err "hermes 核心依赖 import 失败 —— extras 可能顶掉了某个依赖"
    fi

    if [ -f "$FREEZE_POST" ]; then
        # 只报"版本有变化"的，新增的不算问题
        CHANGED="$(join -j 1 \
            <(sed 's/==/ /' "$FREEZE_PRE" | sort -k1,1) \
            <(sed 's/==/ /' "$FREEZE_POST" | sort -k1,1) 2>/dev/null \
            | awk '$2 != $3 {print "      " $1 ": " $2 " -> " $3}' || true)"
        if [ -n "$CHANGED" ]; then
            log_warn "以下已有依赖的版本被改动了（新装的包不算）："
            printf '%s\n' "$CHANGED"
            log_info "若不满意：重跑主包 install.sh --force 会重建 venv"
        else
            log_ok "hermes 原有依赖的版本一个都没被改动"
        fi
    fi

    # ── 8.2 MCP 服务器真实握手 ──
    if [ "$SKIP_MCP" = false ] && [ -f "$MCP_DIR/servers.json" ]; then
        if "$VPY" "$BUNDLE_DIR/verify-mcp.py" --servers "$MCP_DIR/servers.json" \
                --timeout 60; then
            log_ok "MCP 服务器全部握手成功"
        else
            log_warn "有 MCP 服务器没通过握手（细节见上）。hermes 仍可正常用，"
            log_warn "失败的 server 在启动日志里会有 warning，不影响其它工具。"
        fi
    fi

    # ── 8.3 技能索引 ──
    if [ "$SKIP_SKILLS" = false ]; then
        n_skills=$(find "$HERMES_HOME/skills" -name SKILL.md 2>/dev/null | wc -l)
        log_ok "$HERMES_HOME/skills 下共 $n_skills 个技能"
    fi
fi

cat > "$INSTALL_DIR/.extras-installed" <<EOF
installed_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
extras_bundle=$(basename "$BUNDLE_DIR")
install_dir=$INSTALL_DIR
hermes_home=$HERMES_HOME
mcp_venv=$MCP_VENV
mcp_node=$MCP_DIR/node
wheelhouse=$WHEELHOUSE
hermes_ok=$HERMES_OK
EOF

# =============================================================================
# 完成
# =============================================================================
echo ""
echo -e "${C_BOLD}─────────────────────────────────────────────────────${C_OFF}"
if [ "$HERMES_OK" = true ]; then
    log_ok "增强包安装完成"
else
    log_err "增强包装上了，但 hermes 自检未通过 —— 请先处理上面的报错"
fi
echo ""
echo "  安装位置"
echo "    Python 包   → $VENV"
echo "    MCP venv    → $MCP_VENV"
echo "    MCP node    → $MCP_DIR/node"
echo "    配置        → $HERMES_HOME/config.yaml（mcp_servers 节）"
echo ""
echo "  下一步"
echo "    hermes                        # 进 CLI，MCP 工具会自动加载"
echo "    hermes dashboard              # 或者开浏览器控制台"
echo "    $VPY $INSTALL_DIR/hermes skills list"
echo ""
echo "  想砍掉某些 MCP 服务器，注释掉 $HERMES_HOME/config.yaml 里对应的条目即可"
echo "  （每个 server 都会拉一个子进程，数量多会拖慢启动）"
echo ""
exit 0
