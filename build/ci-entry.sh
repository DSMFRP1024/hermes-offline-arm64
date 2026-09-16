#!/bin/bash
# =============================================================================
# CI 容器内入口脚本（在 quay.io/pypa/manylinux_2_28_aarch64 里执行）
# =============================================================================
#
# 为什么单独抽成一个文件而不是塞进 workflow 的 inline shell：
#   - 可以用 `bash -n` 在本地先验语法，不必等跑一轮 CI 才发现引号写错；
#   - YAML 里的 heredoc / 引号嵌套是最容易出错、又最难读的地方。
#
# 环境变量（由 workflow 传入，均可选）：
#   HERMES_COMMIT     要构建的 hermes-agent 提交（默认见 build_bundle.py）
#   PY_VERSION        目标 Python 版本，默认 3.11
#   NODE_LINE         Node 主版本线，默认 24
#   GLIBC             目标 glibc 基线，默认 2.28
#   PYPI_INDEX        PyPI 镜像（境内本机可填清华源）
#   SKIP_PLAYWRIGHT   非空则跳过 Chromium
#   SKIP_MEDIA        非空则跳过 rg/ffmpeg
#   GH_MIRROR         非空则 GitHub 资源走加速前缀
# =============================================================================

set -euo pipefail

HERMES_COMMIT="${HERMES_COMMIT:-}"
PY_VERSION="${PY_VERSION:-3.11}"
NODE_LINE="${NODE_LINE:-24}"
GLIBC="${GLIBC:-2.28}"

WORK="/work"
HERMES_SRC="$WORK/.ci/hermes-agent"
OUT="$WORK/dist"

echo "================================================================"
echo "  Hermes Agent 离线包 CI 构建（容器内）"
echo "  $(uname -m) / glibc $(getconf GNU_LIBC_VERSION)"
echo "  python=$PY_VERSION  node=$NODE_LINE  glibc=$GLIBC"
echo "================================================================"

# ── 1. 容器依赖 ──
# manylinux 镜像只带编译工具链，git/make/xz 这类要自己装。
if command -v dnf >/dev/null 2>&1; then
    PKG=(dnf install -y)
elif command -v microdnf >/dev/null 2>&1; then
    PKG=(microdnf install -y)
else
    echo "✗ 容器里既没有 dnf 也没有 microdnf，无法装构建依赖" >&2
    exit 1
fi

echo "→ 安装容器构建依赖..."
"${PKG[@]}" git tar xz gzip which findutils diffutils make gcc-c++ >/dev/null 2>&1 \
    || "${PKG[@]}" git tar xz gzip which findutils diffutils make gcc-c++

for tool in git tar xz make g++ sha256sum; do
    command -v "$tool" >/dev/null 2>&1 || { echo "✗ 缺少 $tool" >&2; exit 1; }
done
echo "  ✓ git $(git --version | awk '{print $3}') / g++ $(g++ -dumpversion) / make 就绪"

# ── 2. 检出目标源码 ──
# 刻意不用 --depth 1 后 checkout 任意 SHA：GitHub 对"非分支尖端的浅提交"
# 支持不稳。改为浅克隆 main，再确认尖端是否就是目标 SHA —— 是则直接用，
# 不是才去 fetch 那个具体提交。
PIN="${HERMES_COMMIT:-}"
echo "→ 克隆 hermes-agent..."
mkdir -p "$WORK/.ci"
rm -rf "$HERMES_SRC"
git clone --depth 1 --single-branch --branch main \
    https://github.com/NousResearch/hermes-agent.git "$HERMES_SRC"

HEAD_SHA="$(git -C "$HERMES_SRC" rev-parse HEAD)"
if [ -n "$PIN" ] && [ "$PIN" != "$HEAD_SHA" ]; then
    echo "  分支尖端 $HEAD_SHA != 目标 $PIN，尝试取该提交..."
    if git -C "$HERMES_SRC" fetch --depth 1 origin "$PIN"; then
        git -C "$HERMES_SRC" checkout --detach FETCH_HEAD
        HEAD_SHA="$(git -C "$HERMES_SRC" rev-parse HEAD)"
    else
        echo "  ⚠ 取不到 $PIN，改用分支尖端 $HEAD_SHA 继续"
        PIN=""
    fi
fi
[ -z "$PIN" ] && PIN="$HEAD_SHA"
echo "  ✓ 检出 $HEAD_SHA"

# 把实际构建的提交回传为脚本参数（build-info.json 用）
echo "$HEAD_SHA" > "$WORK/.ci/commit.txt"

# ── 3. 跑构建器 ──
PY_BIN="/opt/python/cp311-cp311/bin/python"
if [ "$PY_VERSION" != "3.11" ]; then
    PY_BIN="/opt/python/cp${PY_VERSION//./}-cp${PY_VERSION//./}/bin/python"
fi
[ -x "$PY_BIN" ] || { echo "✗ 容器里找不到 $PY_BIN" >&2; ls /opt/python >&2; exit 1; }

echo "→ 准备构建用 pip..."
"$PY_BIN" -m pip install --quiet --upgrade pip 2>/dev/null || true
"$PY_BIN" --version

ARGS=(--native --repo "$HERMES_SRC" --out "$OUT"
      --python "$PY_VERSION" --node-line "$NODE_LINE" --glibc "$GLIBC"
      --hermes-commit "$HEAD_SHA" --tarball)
[ -n "${PYPI_INDEX:-}" ] && ARGS+=(--index-url "$PYPI_INDEX")
[ -n "${SKIP_PLAYWRIGHT:-}" ] && ARGS+=(--skip-playwright)
[ -n "${SKIP_MEDIA:-}" ] && ARGS+=(--skip-media)
[ -n "${GH_MIRROR:-}" ] && ARGS+=(--gh-mirror)

echo "→ 开始构建..."
cd "$WORK"
"$PY_BIN" build/build_bundle.py "${ARGS[@]}"

# ── 4. 收尾 ──
mkdir -p "$OUT"
chmod -R a+rX "$OUT" 2>/dev/null || true

echo ""
echo "================ 构建产物 ================"
ls -la "$OUT" | sed 's/^/  /'
if [ -f "$OUT/hermes-offline-arm64.tar.gz" ]; then
    echo ""
    echo "  tar.gz sha256: $(sha256sum "$OUT/hermes-offline-arm64.tar.gz" | awk '{print $1}')"
    echo "  tar.gz 大小  : $(du -h "$OUT/hermes-offline-arm64.tar.gz" | cut -f1)"
fi
echo "  bundle 大小  : $(du -sh "$OUT/hermes-offline-arm64" 2>/dev/null | cut -f1)"
echo "  wheel 数量   : $(find "$OUT/hermes-offline-arm64" -name '*.whl' 2>/dev/null | wc -l)"
echo "  文件总数     : $(find "$OUT/hermes-offline-arm64" -type f 2>/dev/null | wc -l)"
echo "=========================================="
