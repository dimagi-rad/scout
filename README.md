# Scout - Data Agent Platform

A self-hosted platform for deploying AI agents that can query project-specific PostgreSQL databases. Each project gets an isolated agent with its own system prompt, database access scope, and auto-generated data dictionary.

## Features

- **Project Isolation**: Each project connects to its own database with encrypted credentials, read-only connections, and schema-level access control
- **Knowledge Layer**: Table metadata, canonical metrics, verified queries, business rules
- **Self-Learning**: Agent learns from errors and applies corrections to future queries
- **Rich Artifacts**: Interactive dashboards, charts, and reports via sandboxed React components
- **Recipe System**: Save and replay successful analysis workflows
- **MCP Data Layer**: Model Context Protocol server for structured, secure data access
- **Multi-Provider OAuth**: Supports Google, GitHub, CommCare, and CommCare Connect
- **Streaming Chat**: Real-time streaming responses via Server-Sent Events

## Tech Stack

- **Backend**: Django 5 (ASGI), LangGraph, LangChain, Anthropic Claude
- **MCP Server**: Model Context Protocol server for tool-based data access (SQL execution, metadata)
- **Frontend**: React 19, Vite, Tailwind CSS 4, Zustand, Vercel AI SDK v6
- **Database**: PostgreSQL with per-project connection pooling
- **Cache/Queue**: Redis (caching, rate limiting, Celery broker)
- **Auth**: Session cookies, django-allauth (Google, GitHub, CommCare, CommCare Connect)

## Quick Start

### Prerequisites

Install the following tools before cloning:

| Tool | Install |
|------|---------|
| [uv](https://docs.astral.sh/uv/) | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| [direnv](https://direnv.net/) | `brew install direnv` (macOS) or see [direnv docs](https://direnv.net/docs/installation.html) |
| [Bun](https://bun.sh/) | `curl -fsSL https://bun.sh/install \| bash` |
| [invoke](https://www.pyinvoke.org/) | Installed automatically via `uv sync` |

You also need a running **PostgreSQL 14+** and **Redis** instance (or use `inv deps` to start them via Docker).

### 1. Clone and allow direnv

```bash
git clone <repo-url> scout && cd scout
direnv allow   # loads .env and activates the uv virtualenv automatically
```

### 2. Install Python dependencies

```bash
uv sync
```

### 3. Configure environment

```bash
cp .env.example .env
# Edit .env — at minimum set DATABASE_URL, DJANGO_SECRET_KEY,
# ANTHROPIC_API_KEY, and DB_CREDENTIAL_KEY
```

### 4. Install frontend dependencies

```bash
inv frontend-install   # runs: cd frontend && bun install
```

### 5. Start PostgreSQL and Redis

Install PostgreSQL 14+ and Redis via your platform's package manager (e.g. `apt`, `brew`, Postgres.app) and ensure both are running. Then create the database:

```bash
createdb agent_platform
```

Alternatively, use Docker for just the backing services:

```bash
inv deps   # docker compose up platform-db redis
```

### 6. Run migrations

```bash
inv migrate
```

### 7. Create a superuser

```bash
inv createsuperuser   # prompts for email and password
```

### 8. Start all dev servers

```bash
inv dev   # Django :8000, MCP :8100, Vite :5173
```

Open http://localhost:5173 in your browser.

### Docker (alternative)

```bash
docker compose up --build
```

This starts five services: backend API (port 8000), frontend (port 3000), MCP server (port 8100), PostgreSQL, and Redis.

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
+---------------+-------------------+------------------------+
                |                   |
+---------------v------+  +--------v-------------------------+
|  LangGraph Agent     |  |  MCP Server (:8100)              |
|  - Self-correction   |  |  - SQL execution & validation    |
|  - Artifact creation |  |  - Table metadata & discovery    |
|  - PG checkpointer   |  |  - Response envelope & audit log |
+---------------+------+  +--------+-------------------------+
                |                   |
+---------------v-------------------v------------------------+
|          PostgreSQL (per-project isolation)                 |
|  Encrypted credentials, read-only, schema-scoped           |
+------------------------------------------------------------+
                Redis (caching, rate limiting, Celery broker)
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
