#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PACKAGE_NAME="factory-fee-windows-local-v1-offline-py312"
BUILD_DIR="$ROOT_DIR/dist/$PACKAGE_NAME"
ZIP_PATH="$ROOT_DIR/dist/$PACKAGE_NAME.zip"
WHEELHOUSE="$BUILD_DIR/wheelhouse"

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
  --exclude "wheelhouse/" \
  --exclude "data/*.db" \
  --exclude "data/*.db-shm" \
  --exclude "data/*.db-wal" \
  --exclude "data/acceptance_v1/" \
  --exclude "data/output/*" \
  --exclude "outputs/*"

mkdir -p "$BUILD_DIR/data/output" "$BUILD_DIR/outputs" "$WHEELHOUSE"

python3 -m pip download \
  --dest "$WHEELHOUSE" \
  --only-binary=:all: \
  --platform win_amd64 \
  --implementation cp \
  --python-version 312 \
  --abi cp312 \
  -r "$ROOT_DIR/requirements-runtime.txt"

(
  cd "$ROOT_DIR/dist"
  zip -qr "$PACKAGE_NAME.zip" "$PACKAGE_NAME"
)

echo "Windows offline deployment package created:"
echo "$ZIP_PATH"
