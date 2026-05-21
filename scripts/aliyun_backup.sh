#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

ENV_FILE="${ENV_FILE:-$ROOT_DIR/.env}"
COMPOSE_FILE="${COMPOSE_FILE:-$ROOT_DIR/docker-compose.aliyun.yml}"
BACKUP_DIR="${BACKUP_DIR:-$ROOT_DIR/runtime/backups}"
TS="$(date '+%Y%m%d_%H%M%S')"
ARCHIVE="$BACKUP_DIR/factory_fee_runtime_${TS}.tar.gz"

if [ -f "$ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
fi

mkdir -p "$BACKUP_DIR"

echo "== Create SQLite online backup when the container is running"
compose_args=(-f "$COMPOSE_FILE")
if [ -f "$ENV_FILE" ]; then
  compose_args+=(--env-file "$ENV_FILE")
fi

running_container="$(docker compose "${compose_args[@]}" ps --status running -q factory-fee 2>/dev/null || true)"
if [ -n "$running_container" ]; then
  docker compose "${compose_args[@]}" exec -T -e BACKUP_TS="$TS" factory-fee python - <<'PY'
import os
import sqlite3
from pathlib import Path

data_dir = Path("/app/runtime/data")
source = data_dir / "factory_fee.db"
target_dir = data_dir / "_sqlite_backups"
target_dir.mkdir(parents=True, exist_ok=True)
target = target_dir / f"factory_fee_{os.environ['BACKUP_TS']}.db"
if source.exists():
    src = sqlite3.connect(source)
    dst = sqlite3.connect(target)
    with dst:
        src.backup(dst)
    src.close()
    dst.close()
    print(f"SQLite backup: {target}")
else:
    print("SQLite database does not exist yet; skip database backup.")
PY
else
  echo "Container is not running; skip SQLite online backup."
fi

echo "== Archive runtime directory"
tar --exclude="runtime/backups" -czf "$ARCHIVE" -C "$ROOT_DIR" runtime
echo "Backup archive: $ARCHIVE"

retention_days="${BACKUP_RETENTION_DAYS:-14}"
find "$BACKUP_DIR" -type f -name "factory_fee_runtime_*.tar.gz" -mtime +"$retention_days" -delete
echo "Retention: ${retention_days} days"
