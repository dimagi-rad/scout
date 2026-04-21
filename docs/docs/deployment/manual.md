# Manual deployment

For environments where Docker is not available or not desired, you can run Scout manually with uvicorn for the backend and a static build for the frontend.

## Prerequisites

- Python 3.12+
- PostgreSQL 14+
- Redis
- Node.js 18+ or Bun
- [uv](https://docs.astral.sh/uv/)
- A reverse proxy (nginx, Caddy) for production

## Backend

### Install dependencies

```bash
uv sync --no-dev
```

### Configure environment

Create a `.env` file or export environment variables. See [Configuration](configuration.md) for the full reference.

### Run migrations

```bash
uv run manage.py migrate
uv run manage.py collectstatic --noinput
```

### Start uvicorn

```bash
DJANGO_SETTINGS_MODULE=config.settings.production \
  uv run uvicorn config.asgi:application \
  --host 0.0.0.0 \
  --port 8000 \
  --workers 4 \
  --lifespan off
```

For production, consider running uvicorn behind a process manager like systemd or supervisord.

## MCP Server

The MCP server runs as a separate process and provides tool-based data access (SQL execution, table metadata) to the LangGraph agent.

```bash
DJANGO_SETTINGS_MODULE=config.settings.production \
  uv run python -m mcp_server
```

By default it listens on port 8100. Set `MCP_SERVER_URL` on the backend to point to the MCP server if it runs on a different host.

## Frontend

### Build the production bundle

```bash
cd frontend
bun install
bun run build
```

This produces a static build in `frontend/dist/`.

### Serve the frontend

Serve `frontend/dist/` with nginx, Caddy, or any static file server. Configure the reverse proxy to:

1. Serve static files from `frontend/dist/` for the root path.
2. Proxy `/api/*` and `/admin/*` requests to the uvicorn backend on port 8000.
3. Handle TLS termination.
4. The MCP server (port 8100) does not need external access — only the backend connects to it.

### Example nginx configuration

```nginx
server {
    listen 443 ssl;
    server_name scout.example.com;

    ssl_certificate /etc/ssl/certs/scout.pem;
    ssl_certificate_key /etc/ssl/private/scout.key;

    # Frontend static files
    location / {
        root /path/to/frontend/dist;
        try_files $uri $uri/ /index.html;
    }

    # Backend API and admin
    location /api/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # SSE support for chat streaming
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 300s;
    }

    location /admin/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }

    location /health/ {
        proxy_pass http://127.0.0.1:8000;
    }

    # Django static files (admin CSS/JS)
    location /static/ {
        alias /path/to/scout/staticfiles/;
    }
}
```

Key points for the proxy configuration:

- **Disable buffering** for `/api/chat/` -- the streaming chat endpoint uses Server-Sent Events, which requires `proxy_buffering off`.
- **Increase read timeout** -- chat responses can take time to generate.
