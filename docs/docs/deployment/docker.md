# Docker deployment

The checked-in Docker Compose configuration runs the local API, frontend, and
backing services. It uses development settings; it is not a hardened production
deployment configuration.

## Quick start

```bash
docker compose up --build
```

This starts five services:

| Service | Port | Description |
|---------|------|-------------|
| Backend (api) | 8000 | Django ASGI server via uvicorn |
| Frontend | 3000 | React app served via nginx |
| MCP Server | 8100 (local override) | Model Context Protocol server for data access |
| PostgreSQL | 5432 | Scout's internal database |
| Cube | 4000 / 4010 | Semantic query API / schema validator |

`docker-compose.override.yml` is automatically loaded locally and publishes
MCP's port. Without that override, MCP is accessible only inside the Compose
network. Procrastinate uses PostgreSQL for jobs, not Redis. For multi-worker or
multi-process production deployments, configure a shared Redis cache through
[`REDIS_URL`](configuration.md#cache) so login lockouts and rate limits share state.

## Configuration

Create a `.env` file in the project root with the required environment variables before running `docker compose up`. See [Configuration](configuration.md) for the full reference.

At minimum:

```
DJANGO_SECRET_KEY=your-secret-key
ANTHROPIC_API_KEY=sk-ant-...
DB_CREDENTIAL_KEY=your-fernet-key
PLATFORM_DB_PASSWORD=your-local-database-password
CUBEJS_API_SECRET=your-local-cube-signing-secret
```

Compose configures the containers' `DATABASE_URL`, `CUBE_API_URL`, and
`CUBE_VALIDATOR_URL` using internal service names (`platform-db` and `cube`).
Do not use those hostnames for host-run processes: follow the
[installation guide](../getting-started/installation.md) for localhost URLs.
Scout and Cube must share the same `CUBEJS_API_SECRET`.

## Background jobs

The five-service stack does not include a persistent worker service. Once the
API has applied its migrations, run a worker in another terminal to process
materialization and chat-resume jobs:

```bash
docker compose run --rm --no-deps api python manage.py procrastinate worker
```

Keep the worker running while testing jobs. This is not necessary if you are
using the host-based Honcho setup, which already starts a worker.

## Persistent data

The PostgreSQL data directory is mounted as a Docker volume to persist data across container restarts. Conversation history (stored via the PostgreSQL checkpointer) and all project configuration survive restarts.

## Health check

The backend exposes `/health/`. The semantic runtime and validator have separate
readiness checks at `http://localhost:4000/readyz` and
`http://localhost:4010/readyz` (or your configured ports). Check both; a healthy
API alone does not prove that semantic queries can run.

## Production considerations

- Use `config.settings.production` for production processes, not the development
  settings pinned in the local Compose file.
- Set `DJANGO_ALLOWED_HOSTS` to your domain name(s).
- Set `CSRF_TRUSTED_ORIGINS` to your frontend's origin.
- Use a strong, unique `DJANGO_SECRET_KEY`.
- Consider placing a reverse proxy (nginx, Caddy) in front for TLS termination.
- Set `MCP_SERVER_URL` if the MCP server runs on a different host (defaults to `http://localhost:8100/mcp`).
- Keep PostgreSQL, MCP, Cube, and the validator on private interfaces. Do not
  publish the local-development ports publicly.
- Run a persistent Procrastinate worker with the same database and semantic
  runtime configuration as the API.
