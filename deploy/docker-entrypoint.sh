#!/usr/bin/env sh
set -eu

RUNTIME_DIR="${FACTORY_FEE_RUNTIME_DIR:-/app/runtime}"
RUNTIME_CONFIG_DIR="$RUNTIME_DIR/config"
RUNTIME_DATA_DIR="$RUNTIME_DIR/data"

mkdir -p \
  "$RUNTIME_CONFIG_DIR" \
  "$RUNTIME_DATA_DIR/input" \
  "$RUNTIME_DATA_DIR/output" \
  "$RUNTIME_DATA_DIR/config_tables" \
  "$RUNTIME_DATA_DIR/periods" \
  "$RUNTIME_DATA_DIR/_sqlite_backups"

if [ ! -f "$RUNTIME_CONFIG_DIR/config.production.yaml" ]; then
  cp -a /app/config/. "$RUNTIME_CONFIG_DIR/"
else
  for file in config.production.yaml config.yaml merge_flows.yaml rule_tables.yaml config_table_versions.yaml processing_fee_exports.yaml; do
    if [ -f "/app/config/$file" ] && [ ! -f "$RUNTIME_CONFIG_DIR/$file" ]; then
      cp "/app/config/$file" "$RUNTIME_CONFIG_DIR/$file"
    fi
  done
fi

exec "$@"
