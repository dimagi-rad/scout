# Installation

## Prerequisites

- Python 3.12+ (3.11+ supported)
- PostgreSQL 14+
- Redis
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

Create an environment file from the example and edit it with your settings:

```bash
cp .env.example .env
```

At minimum, set these variables in `.env`:

```
DATABASE_URL=postgresql://user:password@localhost/scout
DJANGO_SECRET_KEY=your-secret-key
ANTHROPIC_API_KEY=sk-ant-...
DB_CREDENTIAL_KEY=your-fernet-key
```

Generate a Fernet encryption key for database credential storage:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Run migrations and create a superuser:

```bash
uv run manage.py migrate
uv run manage.py collectstatic --noinput
uv run manage.py createsuperuser
```

Start the ASGI development server:

```bash
uv run uvicorn config.asgi:application --host 127.0.0.1 --port 8000 --reload
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

Instead of managing three terminals, use [honcho](https://honcho.readthedocs.io/) to start Django, the MCP server, and Vite together with a single command:

```bash
uv run honcho -f Procfile.dev start
```

Each process is color-coded and labeled in the output. Ctrl+C stops all three.

## Docker setup

If you prefer Docker:

```bash
docker compose up --build
```

This starts five services: backend API (port 8000), frontend (port 3000), MCP server (port 8100), PostgreSQL, and Redis.

## Verify the installation

1. Open `http://localhost:5173` (or `http://localhost:3000` with Docker).
2. Log in with the superuser account you created (or sign up for a new account).
3. If you see the chat interface, the installation is working.

## Next step

[Connect a CommCare domain](dev-testing.md) to load case data and start querying.
