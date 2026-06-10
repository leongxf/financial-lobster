#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

# 按层级清理 storage，层级越高清得越多，且为累积关系（高档 = 低档 + 额外目录）。
#   L1 最初级缓存      cache, tasks                       —— 零能力损失
#   L2 大部分缓存      + embeddings, events               —— 追问降级，可重传恢复
#   L3 除 uploads 外   + conversations                    —— ⚠️ 删用户会话数据
#   L4 整个 storage    + uploads（实为清空 storage 根）   —— ⚠️ 全删，等于重置
LEVEL=1
ASSUME_YES=false
DRY_RUN=false

log() {
  printf '[clear-cache] %s\n' "$*"
}

fail() {
  printf '[clear-cache] ERROR: %s\n' "$*" >&2
  exit 1
}

usage() {
  cat <<'EOF'
用法：./scripts/clear_cache.sh [-l N] [-y] [-n]

按层级清理 storage（层级越高清得越多，且为累积关系）：

  -l 1  最初级缓存（默认）   cache, tasks
        纯缓存 + 日志，零能力损失；重传同文件会重算分析（仅花 token）。
  -l 2  大部分缓存            = 1 + embeddings, events
        额外清向量缓存与去重标记；追问会降级为关键字检索，需重传恢复。
  -l 3  除 uploads 外全部     = 2 + conversations
        额外清会话数据：⚠️ 删除所有用户的聊天历史与文件索引（不可恢复），
        但保留已上传文件与解析结果（uploads）。
  -l 4  清空整个 storage      = 3 + uploads
        ⚠️ 删除全部数据，含原始文件 / 解析结果 / 报告。等于重置。

选项：
  -l, --level N    清理层级 1-4（默认 1）
  -y, --yes        跳过确认，直接清理
  -n, --dry-run    只展示将清理的目录与占用，不实际删除
  -h, --help       显示本帮助
EOF
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      -l|--level)
        [[ $# -ge 2 ]] || fail "$1 需要一个层级值（1-4）"
        LEVEL="$2"
        shift 2
        ;;
      -y|--yes)
        ASSUME_YES=true
        shift
        ;;
      -n|--dry-run)
        DRY_RUN=true
        shift
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      *)
        fail "未知参数：$1（可用：-l/--level N, -y/--yes, -n/--dry-run, -h/--help）"
        ;;
    esac
  done

  case "${LEVEL}" in
    1|2|3|4) ;;
    *) fail "无效层级：${LEVEL}（取值 1-4）" ;;
  esac
}

# 累积式目录集合：高档包含低档的全部目录。
dirs_for_level() {
  local base="storage/cache storage/tasks"
  case "$1" in
    1) echo "$base" ;;
    2) echo "$base storage/embeddings storage/events" ;;
    3) echo "$base storage/embeddings storage/events storage/conversations" ;;
    4) echo "$base storage/embeddings storage/events storage/conversations storage/uploads" ;;
  esac
}

describe_dir() {
  case "$1" in
    storage/cache)         echo "LLM 分片分析缓存（重传重算，花 token）" ;;
    storage/tasks)         echo "任务状态日志（仅丢历史记录）" ;;
    storage/embeddings)    echo "向量缓存（删后追问降级关键字，重传恢复）" ;;
    storage/events)        echo "飞书事件去重标记（删后重推可能重复处理）" ;;
    storage/conversations) echo "⚠️ 会话历史 + 文件索引（用户数据，不可恢复）" ;;
    storage/uploads)       echo "⚠️ 原始文件 + 解析结果 + 报告（用户数据，不可恢复）" ;;
    *)                     echo "" ;;
  esac
}

# L3 起会删除用户数据（conversations / uploads），需要更强的确认提示。
is_data_loss() {
  [[ "${LEVEL}" -ge 3 ]]
}

show_targets() {
  log "层级 ${LEVEL}，将清理以下目录："
  local found=false
  local dir
  for dir in $(dirs_for_level "${LEVEL}"); do
    if [[ -d "$dir" ]]; then
      found=true
      printf '  %-22s %-8s %s\n' "$dir" "$(du -sh "$dir" 2>/dev/null | cut -f1)" "$(describe_dir "$dir")"
    else
      printf '  %-22s %-8s %s\n' "$dir" "(不存在)" "$(describe_dir "$dir")"
    fi
  done
  if [[ "$found" == "false" ]]; then
    log "上述目录均不存在，无需清理。"
    exit 0
  fi
}

confirm() {
  if [[ "$ASSUME_YES" == "true" ]]; then
    return
  fi
  if is_data_loss; then
    log "⚠️ 层级 ${LEVEL} 会删除用户数据（会话历史 / 原始文件），不可恢复！"
  fi
  printf '[clear-cache] 确认清理？此操作不可恢复 [y/N] '
  local reply
  read -r reply
  case "$reply" in
    y|Y|yes|YES) ;;
    *) fail "已取消。" ;;
  esac
}

clear_dirs() {
  if [[ "${LEVEL}" -eq 4 ]]; then
    # L4 = 清空整个 storage 根（含我们未单列的任何残留），再建空目录避免应用启动报错。
    rm -rf "storage"
    mkdir -p "storage"
    log "已清空整个 storage。"
    return
  fi
  local dir
  for dir in $(dirs_for_level "${LEVEL}"); do
    [[ -d "$dir" ]] || continue
    # 删除目录内容后保留空目录，避免应用启动时因缺目录报错。
    rm -rf "${dir:?}/"* "${dir:?}/".[!.]* 2>/dev/null || true
    log "已清理：$dir"
  done
}

main() {
  parse_args "$@"
  show_targets
  if [[ "$DRY_RUN" == "true" ]]; then
    log "dry-run：未删除任何文件。"
    exit 0
  fi
  confirm
  clear_dirs
  log "清理完成（层级 ${LEVEL}）。"
}

main "$@"
