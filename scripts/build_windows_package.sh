#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PACKAGE_NAME="factory-fee-windows-local-v1"
BUILD_DIR="$ROOT_DIR/dist/$PACKAGE_NAME"
ZIP_PATH="$ROOT_DIR/dist/$PACKAGE_NAME.zip"

rm -rf "$BUILD_DIR" "$ZIP_PATH"
mkdir -p "$BUILD_DIR"

rsync -a "$ROOT_DIR/" "$BUILD_DIR/" \
  --exclude ".git/" \
  --exclude ".venv/" \
  --exclude ".netlify/" \
  --exclude ".pytest_cache/" \
  --exclude "__pycache__/" \
  --exclude "*.pyc" \
  --exclude ".DS_Store" \
  --exclude "dist/" \
  --exclude "data/*.db" \
  --exclude "data/*.db-shm" \
  --exclude "data/*.db-wal" \
  --exclude "data/acceptance_v1/" \
  --exclude "data/output/*" \
  --exclude "outputs/*"

mkdir -p "$BUILD_DIR/data/output" "$BUILD_DIR/outputs"

(
  cd "$ROOT_DIR/dist"
  zip -qr "$PACKAGE_NAME.zip" "$PACKAGE_NAME"
)

echo "Windows deployment package created:"
echo "$ZIP_PATH"
