#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

ENV_FILE="${ENV_FILE:-$ROOT_DIR/.env}"
COMPOSE_FILE="${COMPOSE_FILE:-$ROOT_DIR/docker-compose.aliyun.yml}"
ARCHIVE="${1:-}"
compose_args=(-f "$COMPOSE_FILE")
if [ -f "$ENV_FILE" ]; then
  compose_args+=(--env-file "$ENV_FILE")
fi

if [ -z "$ARCHIVE" ]; then
  echo "Usage: CONFIRM_RESTORE=YES scripts/aliyun_restore.sh /path/to/factory_fee_runtime_YYYYMMDD_HHMMSS.tar.gz"
  exit 1
fi

if [ ! -f "$ARCHIVE" ]; then
  echo "Backup archive not found: $ARCHIVE"
  exit 1
fi

if [ "${CONFIRM_RESTORE:-}" != "YES" ]; then
  echo "Restore is destructive. Re-run with CONFIRM_RESTORE=YES."
  exit 1
fi

echo "== Backup current runtime before restore"
if [ -d "$ROOT_DIR/runtime" ]; then
  scripts/aliyun_backup.sh || true
fi

echo "== Stop service"
docker compose "${compose_args[@]}" down || true

echo "== Restore runtime from archive"
rm -rf "$ROOT_DIR/runtime"
tar -xzf "$ARCHIVE" -C "$ROOT_DIR"

echo "== Start service"
docker compose "${compose_args[@]}" up -d

echo "Restore completed."
