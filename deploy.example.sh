#!/usr/bin/env bash
# Copy to deploy.sh locally, then configure deployment through environment variables.
set -euo pipefail

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
bot_dir="$project_dir/apps/bot"
target="${1:-}"
deploy_host="${DEPLOY_HOST:-}"
deploy_dir="${DEPLOY_DIR:-}"
deploy_ssh_port="${DEPLOY_SSH_PORT:-22}"
deploy_ssh_key="${DEPLOY_SSH_KEY:-}"
archive_dir="${DEPLOY_ARCHIVE_DIR:-/tmp}"

usage() {
  cat <<'EOF'
Usage: ./deploy.sh <target>

Targets:
  bot    Sync DMIT knowledge, then rebuild and restart only misaka-bot.
  audit  Sync DMIT knowledge, then rebuild and restart audit-api and audit-ui.
  emby   Rebuild and restart only emby-service.
  all    Sync DMIT knowledge, then rebuild and restart every application service.

Required configuration:
  DEPLOY_HOST       SSH destination, for example deploy@example.com
  DEPLOY_DIR        Existing Compose directory on the deployment host

Optional configuration:
  DEPLOY_SSH_PORT   SSH port; defaults to 22
  DEPLOY_SSH_KEY    Absolute path to the local SSH private key
  DEPLOY_ARCHIVE_DIR Local directory for the temporary archive; defaults to /tmp
EOF
}

require_remote_configuration() {
  if [[ -z "$deploy_host" || -z "$deploy_dir" ]]; then
    echo "Set DEPLOY_HOST and DEPLOY_DIR before deploying." >&2
    exit 64
  fi
}

sync_knowledge() {
  (cd "$bot_dir" && python3 tools/sync_dmit_knowledge.py)
}

build_archive() {
  local archive_path="$1"
  tar -C "$project_dir" -czf "$archive_path" \
    --exclude='.git' \
    --exclude='.venv' \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='.pytest_cache' \
    --exclude='.env' \
    --exclude='data' \
    --exclude='node_modules' \
    --exclude='apps/emby-manager/web/node_modules' \
    --exclude='apps/emby-manager/web/dist' \
    .
}

remote_quote() {
  printf '%q' "$1"
}

remote_directory() {
  local path="$1"
  if [[ "$path" == ~/* ]]; then
    printf '$HOME/%s' "$(remote_quote "${path#~/}")"
  else
    remote_quote "$path"
  fi
}

remote_deploy() {
  local remote_archive="$1"
  local quoted_dir
  local quoted_extract_dir
  quoted_dir="$(remote_directory "$deploy_dir")"
  quoted_extract_dir="$(remote_directory "${deploy_dir%/}/.misakabot-deploy-extract")"

  ssh "${ssh_options[@]}" "$deploy_host" \
    "set -euo pipefail
    find /tmp -maxdepth 1 -type f -name 'misakabot-*.tar.gz' ! -name $(remote_quote "$remote_archive") -delete
    rm -rf $quoted_extract_dir
    mkdir -p $quoted_extract_dir $quoted_dir
    tar -xzf /tmp/$remote_archive -C $quoted_extract_dir
    tar -C $quoted_extract_dir -cf - . | tar -C $quoted_dir -xf -
    rm -rf $quoted_extract_dir
    rm -f /tmp/$remote_archive
    cd $quoted_dir
    case $(remote_quote "$target") in
      bot)
        docker compose build misaka-bot
        docker compose up -d --no-deps --force-recreate misaka-bot
        docker compose logs --tail=200 -f misaka-bot
        ;;
      audit)
        docker compose --profile audit build audit-api audit-ui
        docker compose --profile audit up -d --no-deps --force-recreate audit-api audit-ui
        docker compose --profile audit logs --tail=200 -f audit-api audit-ui
        ;;
      emby)
        docker compose --profile emby build emby-service
        docker compose --profile emby up -d --no-deps --force-recreate emby-service
        docker compose --profile emby logs --tail=200 -f emby-service
        ;;
      all)
        docker compose --profile audit --profile emby build
        docker compose --profile audit --profile emby up -d --force-recreate
        docker compose --profile audit --profile emby logs --tail=200 -f misaka-bot audit-api audit-ui emby-service
        ;;
    esac"
}

case "$target" in
  bot|audit|emby|all) ;;
  -h|--help|"") usage; exit 0 ;;
  *)
    echo "Unknown deployment target: $target" >&2
    usage >&2
    exit 64
    ;;
esac

require_remote_configuration
ssh_options=(-p "$deploy_ssh_port")
scp_options=(-P "$deploy_ssh_port")
if [[ -n "$deploy_ssh_key" ]]; then
  if [[ ! -r "$deploy_ssh_key" ]]; then
    echo "DEPLOY_SSH_KEY does not point to a readable private key file." >&2
    exit 64
  fi
  ssh_options+=(-i "$deploy_ssh_key")
  scp_options+=(-i "$deploy_ssh_key")
fi
if [[ "$target" == "bot" || "$target" == "audit" || "$target" == "all" ]]; then
  sync_knowledge
fi

mkdir -p "$archive_dir"
archive_name="misakabot-${target}-$(date +%Y%m%d%H%M%S)-$$.tar.gz"
archive_path="$archive_dir/$archive_name"
trap 'rm -f "$archive_path"' EXIT

echo "Packaging $target locally…"
build_archive "$archive_path"
echo "Uploading $archive_name to ${deploy_host}…"
scp "${scp_options[@]}" "$archive_path" "$deploy_host:/tmp/$archive_name"
echo "Building and restarting $target on ${deploy_host}…"
remote_deploy "$archive_name"
