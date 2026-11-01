#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
COMMIT_MSG_HOOK="$ROOT_DIR/.githooks/commit-msg"
TASK_BRANCH_PATTERN='^(feature|fix|hotfix|chore)/[0-9]+-[a-z0-9-]+$'
TEMP_DIR=

cleanup() {
  if [[ -n "$TEMP_DIR" ]]; then
    rm -rf -- "$TEMP_DIR"
  fi
}

trap cleanup EXIT

usage() {
  cat >&2 <<'EOF'
用法：
  bash scripts/check-pr-policy.sh title TITLE_FILE
  bash scripts/check-pr-policy.sh commits BASE_SHA HEAD_SHA
  bash scripts/check-pr-policy.sh branch BASE_BRANCH HEAD_BRANCH

模式：
  title    校验 TITLE_FILE 中的 PR 标题。
  commits  校验 BASE_SHA..HEAD_SHA 引入的每个完整提交信息。
  branch   校验从 HEAD_BRANCH 向 BASE_BRANCH 发起 PR 的分支规则。
EOF
}

require_argument_count() {
  local expected=$1
  shift
  if [[ $# -ne "$expected" ]]; then
    usage
    exit 2
  fi
}

check_title() {
  require_argument_count 1 "$@"
  local title_file=$1

  if [[ ! -f "$title_file" || ! -r "$title_file" ]]; then
    printf '错误：标题文件不可读：%s\n' "$title_file" >&2
    exit 2
  fi

  bash "$COMMIT_MSG_HOOK" --title-only "$title_file"
}

check_commits() {
  require_argument_count 2 "$@"
  local base_sha=$1
  local head_sha=$2
  local commit_list
  local message_file
  local failed=0
  local sha
  local short_sha

  if [[ ! "$base_sha" =~ ^[0-9a-fA-F]{40}$ || ! "$head_sha" =~ ^[0-9a-fA-F]{40}$ ]]; then
    printf '%s\n' '错误：BASE_SHA 和 HEAD_SHA 必须是完整的 40 位十六进制提交 SHA。' >&2
    exit 2
  fi
  if ! git cat-file -e "${base_sha}^{commit}" 2>/dev/null; then
    printf '错误：找不到基线提交 %s。请先获取完整历史。\n' "$base_sha" >&2
    exit 2
  fi
  if ! git cat-file -e "${head_sha}^{commit}" 2>/dev/null; then
    printf '错误：找不到 PR 头提交 %s。请先获取完整历史。\n' "$head_sha" >&2
    exit 2
  fi

  TEMP_DIR=$(mktemp -d "${TMPDIR:-/tmp}/check-pr-policy.XXXXXX")
  commit_list="$TEMP_DIR/commits"
  message_file="$TEMP_DIR/message"

  if ! git rev-list --reverse "${base_sha}..${head_sha}" > "$commit_list"; then
    printf '%s\n' '错误：无法计算 PR 提交范围。' >&2
    exit 2
  fi

  while IFS= read -r sha; do
    [[ -n "$sha" ]] || continue
    short_sha=$(git rev-parse --short=12 "$sha")
    if [[ -n "$(git ls-tree -r --name-only "$sha" -- .omp/agents)" ]]; then
      printf '错误：提交 %s 跟踪了禁止入库的 .omp/agents/ 内容。\n' "$short_sha" >&2
      failed=1
    fi
    git show -s --format=%B "$sha" > "$message_file"
    if ! bash "$COMMIT_MSG_HOOK" "$message_file"; then
      printf '错误：提交 %s 的提交信息不符合规范。\n' "$short_sha" >&2
      failed=1
    fi
  done < "$commit_list"

  return "$failed"
}

check_branch() {
  require_argument_count 2 "$@"
  local base_branch=$1
  local head_branch=$2

  case "$base_branch" in
    master)
      case "$head_branch" in
        release/*|hotfix/*)
          return 0
          ;;
        *)
          printf '错误：master 只接受 release/* 或 hotfix/* 分支的 PR，当前来源为 %s。\n' "$head_branch" >&2
          return 1
          ;;
      esac
      ;;
    dev/*)
      case "$head_branch" in
        master|main|develop|dev/*)
          printf '错误：受保护分支 %s 不能作为发往 %s 的普通任务来源。\n' "$head_branch" "$base_branch" >&2
          return 1
          ;;
      esac
      if [[ ! "$head_branch" =~ $TASK_BRANCH_PATTERN ]]; then
        printf '警告：分支名 "%s" 不符合 <类型>/<编号>-<描述> 格式，PR 检查将继续。\n' "$head_branch" >&2
      fi
      ;;
    *)
      printf '错误：不支持的目标分支 %s；只允许 master 或 dev/*。\n' "$base_branch" >&2
      return 1
      ;;
  esac
}

if [[ $# -eq 0 ]]; then
  usage
  exit 2
fi

mode=$1
shift
case "$mode" in
  title)
    check_title "$@"
    ;;
  commits)
    check_commits "$@"
    ;;
  branch)
    check_branch "$@"
    ;;
  *)
    printf '错误：未知模式 %s。\n' "$mode" >&2
    usage
    exit 2
    ;;
esac
