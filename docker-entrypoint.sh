#!/bin/bash
set -euo pipefail

# Run migrations before starting the API server.
# Only runs when the CMD is uvicorn (the API service), not for
# MCP server, Celery worker, or other commands.
if [[ "${1:-}" == "uvicorn" ]]; then
  echo "Running migrations..."
  python manage.py migrate --no-input
fi

exec "$@"
