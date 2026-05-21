#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

ENV_FILE="${ENV_FILE:-$ROOT_DIR/.env}"
COMPOSE_FILE="${COMPOSE_FILE:-$ROOT_DIR/docker-compose.aliyun.yml}"

need_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing command: $1"
    exit 1
  fi
}

random_secret() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 32
  elif command -v sha256sum >/dev/null 2>&1; then
    printf "%s-%s" "$(date "+%s%N")" "$(hostname)" | sha256sum | awk '{print $1}'
  else
    printf "%s-%s" "$(date "+%s%N")" "$(hostname)" | cksum | awk '{print $1}'
  fi
}

need_command docker

if [ ! -f "$ENV_FILE" ]; then
  secret="$(random_secret)"
  sed "s/replace-with-a-long-random-string/$secret/g" "$ROOT_DIR/.env.production.example" > "$ENV_FILE"
  echo "Created .env with a generated secret."
fi

if grep -q "replace-with-a-long-random-string" "$ENV_FILE"; then
  secret="$(random_secret)"
  tmp_file="${ENV_FILE}.tmp"
  sed "s/replace-with-a-long-random-string/$secret/g" "$ENV_FILE" > "$tmp_file"
  mv "$tmp_file" "$ENV_FILE"
  echo "Updated .env with a generated secret."
fi

mkdir -p \
  "$ROOT_DIR/runtime/data/input" \
  "$ROOT_DIR/runtime/data/output" \
  "$ROOT_DIR/runtime/data/config_tables" \
  "$ROOT_DIR/runtime/data/periods" \
  "$ROOT_DIR/runtime/backups"

compose_args=(-f "$COMPOSE_FILE" --env-file "$ENV_FILE")

echo "== Build and start Factory Fee Console"
docker compose "${compose_args[@]}" up -d --build

port="$(grep -E '^FACTORY_FEE_PORT=' "$ENV_FILE" | tail -1 | cut -d= -f2- || true)"
port="${port:-5050}"

echo "== Health check"
for i in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:${port}/healthz" >/dev/null 2>&1; then
    echo "Service is healthy: http://127.0.0.1:${port}"
    exit 0
  fi
  sleep 2
done

echo "Service did not pass health check. Showing logs:"
docker compose "${compose_args[@]}" logs --tail=120 factory-fee
exit 1
