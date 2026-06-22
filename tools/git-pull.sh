#!/usr/bin/env bash
set -euo pipefail

GIT_URL="${1:-}"
BRANCH="${2:-}"
REMOTE_DIR="${3:-}"
LOCAL_DIR="${4:-.}"

if [ -z "$GIT_URL" ] || [ -z "$BRANCH" ] || [ -z "$REMOTE_DIR" ]; then
    echo "用法:"
    echo "  $0 <git地址> <分支名> <项目子目录> [本地目录]"
    echo
    echo "示例:"
    echo "  $0 https://github.com/openwrt/openwrt.git main package/network/services ./services"
    echo "  $0 git@github.com:user/repo.git dev path/to/dir"
    exit 1
fi

if ! command -v git >/dev/null 2>&1; then
    echo "错误: 未安装 git"
    exit 1
fi

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

mkdir -p "$LOCAL_DIR"

echo "Git 地址: $GIT_URL"
echo "分支: $BRANCH"
echo "远程目录: $REMOTE_DIR"
echo "本地目录: $LOCAL_DIR"
echo

cd "$TMP_DIR"

git init -q
git remote add origin "$GIT_URL"

git sparse-checkout init --cone
git sparse-checkout set "$REMOTE_DIR"

git pull --depth=1 origin "$BRANCH"

if [ ! -d "$REMOTE_DIR" ]; then
    echo "错误: 远程目录不存在: $REMOTE_DIR"
    exit 1
fi

# 复制目录内容，包括隐藏文件
(
    shopt -s dotglob nullglob
    cp -a "$REMOTE_DIR"/* "$OLDPWD/$LOCAL_DIR"/
)

echo
echo "完成: 已将 $REMOTE_DIR 的内容拉取到 $LOCAL_DIR"
