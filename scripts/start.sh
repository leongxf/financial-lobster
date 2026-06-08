#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

SKIP_PULL=false

log() {
  printf '[financial-lobster] %s\n' "$*"
}

fail() {
  printf '[financial-lobster] ERROR: %s\n' "$*" >&2
  exit 1
}

usage() {
  cat <<'EOF'
用法：./scripts/start.sh [--no-pull]

  默认会先 git pull 拉取最新代码，再构建并启动 feishu-worker。
  --no-pull  跳过代码更新，仅重建并启动容器。
EOF
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --no-pull)
        SKIP_PULL=true
        shift
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      *)
        fail "未知参数：$1（可用：--no-pull）"
        ;;
    esac
  done
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || fail "未找到命令：$1"
}

compose() {
  if docker compose version >/dev/null 2>&1; then
    docker compose "$@"
  elif command -v docker-compose >/dev/null 2>&1; then
    docker-compose "$@"
  else
    fail "未找到 docker compose，请先安装 Docker Compose"
  fi
}

read_env_value() {
  local key="$1"
  local file="$2"
  local line
  line="$(grep -E "^${key}=" "$file" | tail -n 1 || true)"
  if [[ -z "$line" ]]; then
    printf ''
    return
  fi
  printf '%s' "${line#*=}" | sed -e 's/^["'\'' ]*//' -e 's/["'\'' ]*$//'
}

ensure_env_file() {
  if [[ -f .env ]]; then
    return
  fi

  if [[ ! -f .env.example ]]; then
    fail "缺少 .env 与 .env.example，无法启动"
  fi

  cp .env.example .env
  log "已从 .env.example 生成 .env，请先填写 FEISHU 与 LLM 配置后重新运行"
  exit 1
}

validate_env() {
  local app_id app_secret

  app_id="$(read_env_value FEISHU_APP_ID .env)"
  app_secret="$(read_env_value FEISHU_APP_SECRET .env)"

  if [[ -z "$app_id" || -z "$app_secret" ]]; then
    fail ".env 中 FEISHU_APP_ID / FEISHU_APP_SECRET 不能为空"
  fi
}

prepare_storage() {
  mkdir -p storage/uploads storage/tasks storage/cache
}

update_code() {
  if [[ "$SKIP_PULL" == "true" ]]; then
    log "跳过代码更新（--no-pull）"
    return
  fi

  if [[ ! -d .git ]]; then
    log "当前目录不是 git 仓库，跳过代码更新"
    return
  fi

  require_command git

  if [[ -n "$(git status --porcelain 2>/dev/null || true)" ]]; then
    fail "工作区有未提交改动，请先提交/暂存后再部署，或使用 --no-pull 跳过更新"
  fi

  log "拉取最新代码 ..."
  git pull --ff-only
}

print_status() {
  compose ps
  echo
  log "启动完成（飞书长连接 worker）"
  log "查看日志：docker compose logs -f feishu-worker"
  log "停止服务：docker compose down"
}

main() {
  parse_args "$@"
  require_command docker
  ensure_env_file
  validate_env
  update_code
  prepare_storage
  log "构建并启动 feishu-worker ..."
  compose up -d --build
  print_status
}

main "$@"
