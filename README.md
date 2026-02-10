# Scout - Data Agent Platform

A self-hosted platform for deploying AI agents that can query project-specific PostgreSQL databases. Each project gets an isolated agent with its own system prompt, database access scope, and auto-generated data dictionary.

## Features

- **Project Isolation**: Each project connects to its own database with encrypted credentials, read-only connections, and schema-level access control
- **Knowledge Layer**: Table metadata, canonical metrics, verified queries, business rules
- **Self-Learning**: Agent learns from errors and applies corrections to future queries
- **Rich Artifacts**: Interactive dashboards, charts, and reports via sandboxed React components
- **Recipe System**: Save and replay successful analysis workflows
- **Multi-Provider OAuth**: Supports Google, GitHub, and custom OAuth providers
- **Streaming Chat**: Real-time streaming responses via Server-Sent Events

## Tech Stack

- **Backend**: Django 5 (ASGI), LangGraph, LangChain, Anthropic Claude
- **Frontend**: React, Vite, Tailwind CSS 4, Zustand, Vercel AI SDK v6
- **Database**: PostgreSQL with per-project connection pooling
- **Auth**: Session cookies, django-allauth (Google, GitHub, custom OAuth)

## Quick Start

### Prerequisites

- Python 3.12+ (recommended; 3.11+ supported)
- PostgreSQL
- Node.js 18+ or Bun
- [uv](https://docs.astral.sh/uv/) (Python package manager)

### Backend

```bash
# Install dependencies
uv sync

# Set up environment
cp .env.example .env
# Edit .env with your database URL, secret key, and Anthropic API key

# Run migrations
uv run manage.py migrate

# Collect static files (needed for admin under uvicorn)
uv run manage.py collectstatic --noinput

# Create a superuser
uv run manage.py createsuperuser

# Start the backend (ASGI)
DJANGO_SETTINGS_MODULE=config.settings.development uv run uvicorn config.asgi:application --host 127.0.0.1 --port 8000 --reload
```

### Frontend

```bash
cd frontend
bun install
bun dev
```

The frontend dev server runs on http://localhost:5173 and proxies `/api/*` to the backend.

### Docker

```bash
docker compose up --build
```

This starts the backend (port 8000), frontend (port 3000), and PostgreSQL.

## Project Setup

1. Log in to Django admin at http://localhost:8000/admin/
2. Create a **Project** with database credentials pointing to the target database
3. Add a **ProjectMembership** linking your user to the project
4. Open the frontend and select the project to start chatting

## Architecture

```
+------------------------------------------------------------+
|                  React Frontend (Vite)                      |
|  Vercel AI SDK v6, Zustand, Tailwind CSS 4                 |
+----------------------------+-------------------------------+
                             |
+----------------------------v-------------------------------+
|               Django Backend (ASGI / uvicorn)              |
|  Streaming chat, Auth, Projects API, Artifacts API         |
+----------------------------+-------------------------------+
                             |
+----------------------------v-------------------------------+
|               LangGraph Agent Runtime                      |
|  - Knowledge-grounded SQL generation                       |
|  - Self-correction loop (up to 3 retries)                  |
|  - Artifact creation (charts, dashboards, reports)         |
|  - PostgreSQL checkpointer for conversation persistence    |
+----------------------------+-------------------------------+
                             |
+----------------------------v-------------------------------+
|          PostgreSQL (per-project isolation)                 |
|  Encrypted credentials, read-only, schema-scoped           |
+------------------------------------------------------------+
```

## Security

- **Database isolation**: Each project has its own encrypted DB credentials; connections are read-only with schema-scoped `search_path`
- **SQL validation**: Only SELECT queries allowed; dangerous functions blocked via sqlglot AST analysis
- **Table access control**: Per-project allowlist/blocklist for table access
- **Rate limiting**: Per-user and per-project query quotas
- **Query limits**: Automatic LIMIT injection and statement timeouts
- **Session auth**: Cookie-based sessions with CSRF protection

## License

Proprietary - All rights reserved.
