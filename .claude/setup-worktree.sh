#!/usr/bin/env bash
# Run on SessionStart to set up a fresh git worktree.
# Copies env files from the main worktree and installs dependencies.
# No-ops when run from the main worktree.

set -euo pipefail

ROOT_WORKTREE_PATH=$(git worktree list --porcelain | awk '/^worktree/{print $2; exit}')
CURRENT_PATH=$(git rev-parse --show-toplevel 2>/dev/null || pwd)

if [ "$CURRENT_PATH" = "$ROOT_WORKTREE_PATH" ]; then
    exit 0
fi

echo "[scout] Setting up worktree at $CURRENT_PATH"
export ROOT_WORKTREE_PATH

[ -f "$ROOT_WORKTREE_PATH/.env" ]    && cp "$ROOT_WORKTREE_PATH/.env"    .env
[ -f "$ROOT_WORKTREE_PATH/.envrc" ]  && cp "$ROOT_WORKTREE_PATH/.envrc"  .envrc
command -v direnv &>/dev/null        && direnv allow

uv sync
cd frontend && bun install

echo "[scout] Worktree ready."
