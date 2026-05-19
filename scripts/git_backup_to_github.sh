#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "Not a git repository: $ROOT_DIR"
  exit 1
fi

if ! git remote get-url origin >/dev/null 2>&1; then
  echo "Git remote 'origin' is not configured."
  exit 1
fi

BRANCH="$(git branch --show-current)"
if [ -z "$BRANCH" ]; then
  echo "Detached HEAD is not supported for automatic backup."
  exit 1
fi

echo "== GitHub backup"
echo "Repository: $ROOT_DIR"
echo "Branch: $BRANCH"

git add -A

if git diff --cached --quiet; then
  echo "No local changes to commit."
else
  TS="$(date '+%Y-%m-%d %H:%M:%S')"
  git commit -m "chore: automatic backup ${TS}"
fi

git push origin "$BRANCH"
git push origin --tags

echo "Backup completed."
