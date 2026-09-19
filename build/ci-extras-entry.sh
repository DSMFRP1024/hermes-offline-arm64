#!/bin/bash
# =============================================================================
# 离线增强包 —— CI 容器内入口（在 quay.io/pypa/manylinux_2_28_aarch64 里执行）
# =============================================================================
#
# 为什么单独抽成文件而不是塞进 workflow 的 inline shell：
#   可以用 `bash -n` 在本地先验语法，不必等跑一轮 CI 才发现引号写错。
#
# 这个容器只干一件事：**下载轮子**。它提供 glibc 2.28 基线 + cp311，
# 所以 pip 在这里求值出来的轮子天然就是信创机要的那一份 —— 没有任何
# --platform / --python-version 交叉参数，PEP 508 标记也就不会按宿主求值。
#
# 环境变量（由 workflow 传入，均可选）：
#   PY_VERSION   目标 Python 版本，默认 3.11
#   GLIBC        目标 glibc 基线，默认 2.28（容器本身就是 2_28，改小会审计不过）
#   PYPI_INDEX   PyPI 镜像（境内本机可填清华源）
#   LEAN         非空则跳过 heavy 组（opencv / scipy / jupyterlab 等）
# =============================================================================

set -euo pipefail

PY_VERSION="${PY_VERSION:-3.11}"
GLIBC="${GLIBC:-2.28}"

WORK="/work"
OUT="$WORK/dist/hermes-extras"

echo "================================================================"
echo "  Hermes 离线增强包 CI 构建（容器内 / 只下轮子）"
echo "  $(uname -m) / $(getconf GNU_LIBC_VERSION)"
echo "  python=$PY_VERSION  glibc=$GLIBC  lean=${LEAN:-no}"
echo "================================================================"

# ── 1. 容器依赖 ──
# manylinux 镜像只带编译工具链，tar/which 这类要自己装。
# gcc 是给"只有 sdist 的那几个纯 Python 包"准备的（见 build_extras.py 的 SDIST_PKGS）。
if command -v dnf >/dev/null 2>&1; then
    PKG=(dnf install -y)
elif command -v microdnf >/dev/null 2>&1; then
    PKG=(microdnf install -y)
else
    echo "✗ 容器里既没有 dnf 也没有 microdnf" >&2
    exit 1
fi

echo "→ 安装容器构建依赖..."
"${PKG[@]}" tar gzip which findutils make gcc >/dev/null 2>&1 \
    || "${PKG[@]}" tar gzip which findutils make gcc

for tool in tar gcc which sha256sum; do
    command -v "$tool" >/dev/null 2>&1 || { echo "✗ 缺少 $tool" >&2; exit 1; }
done
echo "  ✓ gcc $(gcc -dumpversion) / tar 就绪"

# ── 2. 定位容器自带的 Python ──
PY_BIN="/opt/python/cp311-cp311/bin/python"
if [ "$PY_VERSION" != "3.11" ]; then
    PY_BIN="/opt/python/cp${PY_VERSION//./}-cp${PY_VERSION//./}/bin/python"
fi
if [ ! -x "$PY_BIN" ]; then
    echo "✗ 容器里找不到 $PY_BIN" >&2
    ls /opt/python >&2
    exit 1
fi

echo "→ 准备 pip..."
"$PY_BIN" -m pip install --quiet --upgrade pip 2>/dev/null || true
"$PY_BIN" --version

# ── 3. 下轮子 ──
ARGS=(wheels --out "$OUT" --python "$PY_BIN" --glibc "$GLIBC")
[ -n "${PYPI_INDEX:-}" ] && ARGS+=(--index "$PYPI_INDEX")
[ -n "${LEAN:-}" ] && ARGS+=(--lean)

echo "→ 开始下载..."
cd "$WORK"
"$PY_BIN" build/build_extras.py "${ARGS[@]}"

# ── 4. 收尾 ──
mkdir -p "$OUT"
chmod -R a+rX "$OUT" 2>/dev/null || true

echo ""
echo "================ 轮子阶段产物 ================"
echo "  wheels      : $(ls -1 "$OUT"/wheels/*.whl 2>/dev/null | wc -l) 个 / $(du -sh "$OUT/wheels" 2>/dev/null | cut -f1)"
echo "  mcp-wheels  : $(ls -1 "$OUT"/mcp-wheels/*.whl 2>/dev/null | wc -l) 个 / $(du -sh "$OUT/mcp-wheels" 2>/dev/null | cut -f1)"
echo "  lock        : $(ls -1 "$OUT"/*.lock.txt 2>/dev/null | wc -l) 份"
echo "=============================================="
