#!/usr/bin/env bash
set -euo pipefail

if ! command -v python3 >/dev/null 2>&1; then
  printf '%s\n' '错误：未找到 python3，无法安装 Git hooks。请安装 Python 3 并确保 python3 在 PATH 中。' >&2
  exit 1
fi

if ! command -v git >/dev/null 2>&1; then
  printf '%s\n' '错误：未找到 git，无法安装 Git hooks。' >&2
  exit 1
fi

script_path=${BASH_SOURCE[0]}
case "$script_path" in
  */*) script_dir=${script_path%/*} ;;
  *) script_dir=. ;;
esac
if ! script_dir=$(CDPATH= cd -- "$script_dir" 2>/dev/null && pwd -P); then
  printf '%s\n' '错误：无法定位安装脚本所在目录。' >&2
  exit 1
fi

if ! repo_root=$(git -C "$script_dir" rev-parse --show-toplevel 2>/dev/null); then
  printf '%s\n' '错误：安装脚本不在 Git 仓库中，未修改任何配置。' >&2
  exit 1
fi
if ! repo_root=$(CDPATH= cd -- "$repo_root" 2>/dev/null && pwd -P); then
  printf '%s\n' '错误：无法解析 Git 仓库根目录。' >&2
  exit 1
fi

hooks_dir=$repo_root/.githooks
if [[ ! -d "$hooks_dir" ]]; then
  printf '%s\n' '错误：仓库中缺少 .githooks 目录，无法安装。' >&2
  exit 1
fi

shopt -s nullglob
hooks=("$hooks_dir"/*)
shopt -u nullglob
if ((${#hooks[@]} == 0)); then
  printf '%s\n' '错误：.githooks 目录中没有可安装的 hook。' >&2
  exit 1
fi

if [[ ! -r "$repo_root/scripts/check-repository-content.py" ]]; then
  printf '%s\n' '错误：缺少共享入库检查脚本，无法安装。' >&2
  exit 1
fi

chmod +x "${hooks[@]}"
git -C "$repo_root" config --local core.hooksPath .githooks

printf '已为仓库 %s 安装 Git hooks。\n' "$repo_root"
printf '%s\n' '配置作用域：仓库本地（core.hooksPath=.githooks）。'
