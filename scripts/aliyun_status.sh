#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

ENV_FILE="${ENV_FILE:-$ROOT_DIR/.env}"
COMPOSE_FILE="${COMPOSE_FILE:-$ROOT_DIR/docker-compose.aliyun.yml}"

if [ -f "$ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
fi

port="${FACTORY_FEE_PORT:-5050}"
compose_args=(-f "$COMPOSE_FILE")
if [ -f "$ENV_FILE" ]; then
  compose_args+=(--env-file "$ENV_FILE")
fi

echo "== Docker status"
docker compose "${compose_args[@]}" ps

echo
echo "== Health"
if curl -fsS "http://127.0.0.1:${port}/healthz"; then
  echo
else
  echo "Health check failed."
fi

echo
echo "== Recent logs"
docker compose "${compose_args[@]}" logs --tail=80 factory-fee
