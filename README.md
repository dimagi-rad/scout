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
- **Semantic Runtime**: Cube (query API and schema validator)
- **Task Queue**: Procrastinate (PostgreSQL-backed; no Redis required)
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

You also need Docker Compose for **PostgreSQL 16** and **Cube** (`inv deps` starts them).

### 1. Clone and allow direnv

```bash
git clone <repo-url> scout && cd scout
direnv allow   # loads .env and activates the uv virtualenv automatically
```

### 2. Install Python dependencies

```bash
uv sync
```

### 3. Install pre-commit hooks

```bash
uv run prek install
```

### 4. Configure environment

```bash
cp -n .env.example .env  # Do not overwrite an existing .env
# Edit .env — at minimum set DATABASE_URL, DJANGO_SECRET_KEY,
# ANTHROPIC_API_KEY, DB_CREDENTIAL_KEY, and the three Cube settings below
```

For local Compose, keep `DATABASE_URL` and `PLATFORM_DB_PASSWORD` consistent
with each other; `.env.example` provides a matching pair. If you change the
password or published PostgreSQL port, update the host URL too. Existing
PostgreSQL volumes keep their original password; editing `.env` does not rotate
it. Restore the matching configuration or explicitly rotate the database
password—do not delete a volume containing data you need.

Returning developers: compare your existing `.env` with `.env.example` and add
`CUBE_API_URL=http://localhost:4000`, `CUBE_VALIDATOR_URL=http://localhost:4010`,
and `CUBEJS_API_SECRET` if missing. Use the same signing secret for the host
processes and the Compose Cube service, which reads it from `.env`. If you
override `CUBE_PORT` or `CUBE_VALIDATOR_PORT`, adjust the URLs accordingly.

### 5. Install frontend dependencies

```bash
inv frontend-install   # runs: cd frontend && bun install
```

### 6. Start PostgreSQL and Cube

```bash
inv deps   # docker compose up -d --build --wait platform-db cube
```

These backing services stay in Docker; do not also launch the Compose MCP/API
when using Honcho below. The host `DATABASE_URL` must point at the published
PostgreSQL port and database. See [local setup details](CLAUDE.md#local-development-setup-including-returning-developers).
After stopping Honcho, `docker compose stop platform-db cube` stops these
dependencies without deleting their data. `inv deps` starts them again.

### 7. Run migrations

```bash
inv migrate
```

### 8. Create a superuser

```bash
inv createsuperuser   # prompts for email and password
```

### 9. Start all dev servers

```bash
inv dev   # Django :8000, MCP :8100, background worker, Vite :5173
```

The web process runs Django system checks before starting and warns about missing
Cube settings. Check both Cube services with `curl --fail http://localhost:4000/readyz`
and `curl --fail http://localhost:4010/readyz`. These checks must succeed before
semantic queries can work.

Open http://localhost:5173 in your browser.

### Docker (alternative)

```bash
docker compose up --build
```

This starts backend API (port 8000), frontend (port 3000), MCP server,
PostgreSQL, and Cube (ports 4000 and 4010). The automatically loaded local
`docker-compose.override.yml` publishes MCP on port 8100; without that override,
MCP is internal to the Compose network. This Compose stack does not start a
background worker; see the [Docker setup guide](docs/docs/deployment/docker.md)
for materialization jobs.

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
                Cube (semantic queries and schema validation)
```

## Security

- **Database isolation**: Each project has its own encrypted DB credentials; connections are read-only with schema-scoped `search_path`
- **Semantic query access**: Agents query curated datasets through structured semantic query specs, not raw SQL
- **Table access control**: Read-only roles and semantic-model visibility control accessible data
- **Rate limiting**: Per-user and per-project query quotas
- **Query limits**: Semantic row limits and statement timeouts
- **Session auth**: Cookie-based sessions with CSRF protection

## License

Proprietary - All rights reserved.
