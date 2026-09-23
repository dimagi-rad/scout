# Installation

## Prerequisites

- Python 3.12+ (3.11+ supported)
- Docker Compose for PostgreSQL 16 and the Cube runtime/schema validator
- Node.js 18+ or [Bun](https://bun.sh/)
- [uv](https://docs.astral.sh/uv/) -- fast Python package manager
- [direnv](https://direnv.net/) (optional, recommended) -- auto-loads `.env` and activates the virtualenv on `cd`. Run `direnv allow` once after cloning.

## Backend setup

Clone the repository and install Python dependencies:

```bash
git clone <repo-url> scout
cd scout
uv sync
```

Install the pre-commit hooks:

```bash
uv run prek install
```

Create an environment file if you do not have one, then edit it with your settings.
Returning developers should add missing settings without replacing their existing file:

```bash
cp -n .env.example .env
```

At minimum, set these variables in `.env`:

```
DATABASE_URL=postgresql://platform:devpassword@localhost:5432/agent_platform
DJANGO_SECRET_KEY=your-secret-key
ANTHROPIC_API_KEY=sk-ant-...
DB_CREDENTIAL_KEY=your-fernet-key
CUBE_API_URL=http://localhost:4000
CUBE_VALIDATOR_URL=http://localhost:4010
CUBEJS_API_SECRET=your-local-cube-signing-secret
```

The database URL above matches Compose's local defaults. If you change
`PLATFORM_DB_PASSWORD` or `PLATFORM_DB_PORT`, update the host URL too. Cube and
the host processes read the same `CUBEJS_API_SECRET` from `.env`; their values
must match. If you change `CUBE_PORT` or `CUBE_VALIDATOR_PORT`, update the
corresponding host URLs.

Generate a Fernet encryption key for database credential storage:

```bash
uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Start the backing services and wait for their health checks:

```bash
docker compose up -d --build --wait platform-db cube
```

PostgreSQL and Cube stay in Docker while the development processes run on the
host. Do not also start Compose's API or MCP server when using Honcho below.
Redis is not required; background jobs use Procrastinate with PostgreSQL.

Run migrations and create a superuser:

```bash
uv run python manage.py migrate
uv run python manage.py collectstatic --noinput
uv run python manage.py createsuperuser
```

To run only the API, start the ASGI development server. For chat and
materialization, use the full Honcho setup below instead:

```bash
uv run uvicorn config.asgi:application --host 127.0.0.1 --port 8000 --reload --lifespan off
```

## Frontend setup

In a separate terminal:

```bash
cd frontend
bun install
bun dev
```

The frontend dev server starts on `http://localhost:5173` and proxies `/api/*` requests to the backend on port 8000.

## Running all dev servers at once

After installing frontend dependencies, use [honcho](https://honcho.readthedocs.io/)
to start Django, the MCP server, the background worker, and Vite together:

```bash
uv run honcho -f Procfile.dev start
```

Each process is color-coded and labeled in the output. Ctrl+C stops these four
host processes, not the Docker dependencies. The web process runs Django system
checks first; missing Cube settings produce `semantic.W001` setup guidance.
This checks configuration only, not service reachability.

## Docker setup

If you prefer Docker:

```bash
docker compose up --build
```

This starts five services: backend API (port 8000), frontend (port 3000), MCP
server (port 8100 with the local override), PostgreSQL, and Cube (ports 4000 and
4010). It does not start a background worker; follow the
[Docker setup guide](../deployment/docker.md) to run materialization jobs.

## Verify the installation

1. Check Cube with `curl --fail http://localhost:4000/readyz` and
   `curl --fail http://localhost:4010/readyz` (or your configured ports).
2. Open `http://localhost:5173` (or `http://localhost:3000` with Docker).
3. Log in with the superuser account you created (or sign up for a new account).
4. Connect a data source using the next guide, materialize it, and run a semantic
   query. Seeing the chat interface alone does not verify the data runtime.

## Next step

[Connect a CommCare domain](dev-testing.md) to load case data and start querying.
