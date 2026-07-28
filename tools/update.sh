#!/usr/bin/env bash
# 更新 Crestron 集成与工具脚本——直接从 GitHub 下载分支压缩包解压，不依赖 git。
# 适合 HAOS「Advanced SSH & Web Terminal」等没有 git 的精简环境。
set -euo pipefail

OWNER_REPO="AfxMsgBox/home-assistant-crestron-component"
# 分支由第一个参数决定，缺省 master。（用 ${1:-...} 兼容 set -u）
BRANCH="${1:-master}"

# 下载器自适应：优先 curl，回退 wget，都没有则报错。
if command -v curl >/dev/null 2>&1; then
    download() { curl -fsSL "$1" -o "$2"; }
elif command -v wget >/dev/null 2>&1; then
    download() { wget -qO "$2" "$1"; }
else
    echo "错误: 需要 curl 或 wget（二者都没有）"
    exit 1
fi

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

echo "分支: $BRANCH （用法: ./update.sh [分支名]，缺省 master）"
URL="https://codeload.github.com/${OWNER_REPO}/tar.gz/refs/heads/${BRANCH}"
echo "下载: $URL"
download "$URL" "$TMP_DIR/src.tar.gz"

echo "解压 ..."
tar -xzf "$TMP_DIR/src.tar.gz" -C "$TMP_DIR"

# GitHub 压缩包顶层是单一目录（名字含分支），用通配取它，避免硬编码目录名，
# 也不依赖 busybox tar 未必支持的 --strip-components。
SRC=""
for d in "$TMP_DIR"/*/; do
    SRC="$d"
    break
done
if [ -z "$SRC" ] || [ ! -d "$SRC" ]; then
    echo "错误: 解压结果异常，未找到源目录"
    exit 1
fi

# 把仓库子目录的内容覆盖到本地目标目录（含隐藏文件；不删上游已移除的文件）。
copy_dir() {
    src_sub="$1"
    dest="$2"
    if [ ! -d "$SRC$src_sub" ]; then
        echo "错误: 压缩包内缺少 $src_sub"
        exit 1
    fi
    mkdir -p "$dest"
    (
        shopt -s dotglob nullglob
        cp -a "$SRC$src_sub"/* "$dest"/
    )
    echo "已更新: $dest"
}

copy_dir "custom_components/crestron" "./custom_components/crestron"
copy_dir "tools" "./tools"

chmod +x ./tools/*.sh ./tools/*.py 2>/dev/null || true

#ha core restart
echo "done!"
