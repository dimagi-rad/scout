#!/usr/bin/env bash
# Start Scout dev environment as a tmux window with 4 panes.
# Logs are teed to /tmp/scout-dev/ so Claude Code can read them.
#
# If run inside an existing tmux session, the dev window is added to that session.
# If run outside tmux, a new "scout-dev" session is created (detached).
#
# Usage:
#   ./scripts/start_dev.sh                    # start everything
#   tmux kill-window -t scout:dev             # stop dev servers

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LOG_DIR="/tmp/scout-dev"
WINDOW_NAME="dev"

# Verify Docker is running
if ! docker info &>/dev/null; then
    echo "ERROR: Docker is not running. Please start Docker and try again."
    exit 1
fi

# Clean up old logs
rm -rf "$LOG_DIR"
mkdir -p "$LOG_DIR"

# Load .env into environment for port variables
if [[ -f "$PROJECT_DIR/.env" ]]; then
    set -a
    source "$PROJECT_DIR/.env"
    set +a
fi

API_PORT="${API_PORT:-8000}"
MCP_PORT="${MCP_PORT:-8100}"
VITE_PORT="${VITE_PORT:-5173}"

# WSL2 doesn't propagate inotify events from /mnt/c (Windows FS).
# Force polling so file watchers (uvicorn, vite, etc.) pick up changes.
if [[ -n "${WSL_DISTRO_NAME:-}" ]]; then
    export WATCHFILES_FORCE_POLLING=1
fi

# Determine which tmux session to use
if [[ -n "${TMUX:-}" ]]; then
    # We're inside tmux — add a window to the current session
    SESSION="$(tmux display-message -p '#S')"
else
    # Not inside tmux — create a standalone detached session
    SESSION="scout-dev"
    tmux kill-session -t "$SESSION" 2>/dev/null || true
    tmux new-session -d -s "$SESSION"
fi

# Kill any existing dev window in this session
tmux kill-window -t "$SESSION:$WINDOW_NAME" 2>/dev/null || true

# Create the dev window with first pane: docker dependencies
tmux new-window -t "$SESSION" -n "$WINDOW_NAME" -c "$PROJECT_DIR" \
    "echo '=== Docker: platform-db + redis ===' && docker compose up platform-db redis 2>&1 | tee '$LOG_DIR/docker.log'; read"

# Split right: API server
tmux split-window -h -t "$SESSION:$WINDOW_NAME" -c "$PROJECT_DIR" \
    "echo '=== API server (:$API_PORT) ===' && sleep 3 && uv run uvicorn config.asgi:application --reload --port $API_PORT 2>&1 | tee '$LOG_DIR/api.log'; read"

# Split bottom-left: MCP server
tmux select-pane -t "$SESSION:$WINDOW_NAME.0"
tmux split-window -v -t "$SESSION:$WINDOW_NAME" -c "$PROJECT_DIR" \
    "echo '=== MCP server (:$MCP_PORT) ===' && sleep 3 && DJANGO_SETTINGS_MODULE=config.settings.development uv run python -m mcp_server --transport streamable-http --port $MCP_PORT --reload 2>&1 | tee '$LOG_DIR/mcp.log'; read"

# Split bottom-right: Frontend
tmux select-pane -t "$SESSION:$WINDOW_NAME.2"
tmux split-window -v -t "$SESSION:$WINDOW_NAME" -c "$PROJECT_DIR/frontend" \
    "echo '=== Frontend (:$VITE_PORT) ===' && sleep 5 && bun dev 2>&1 | tee '$LOG_DIR/frontend.log'; read"

# Even out the layout
tmux select-layout -t "$SESSION:$WINDOW_NAME" tiled

echo ""
echo "Scout dev environment started in window '$WINDOW_NAME' of session '$SESSION'."
echo "  Logs: $LOG_DIR/{docker,api,mcp,frontend}.log"
echo ""
echo "  tmux select-window -t $SESSION:$WINDOW_NAME   # switch to dev window"
echo "  tmux kill-window -t $SESSION:$WINDOW_NAME      # stop everything"
