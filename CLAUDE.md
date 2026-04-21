# Scout

Self-hosted data agent platform for AI-powered database querying.

## Commands

```bash
# Backend
docker compose up platform-db redis mcp-server  # Start dependencies
uv run python manage.py runserver         # Django dev server (or use uvicorn below)
uv run uvicorn config.asgi:application --reload --port 8000 --lifespan off  # ASGI dev server
uv run python manage.py migrate           # Run migrations

# Frontend
cd frontend && bun install && bun dev     # Dev server on :5173
cd frontend && bun run build              # Production build (runs tsc first)

# All dev servers at once (Django :8000, MCP :8100, Vite :5173)
uv run honcho -f Procfile.dev start

# Full stack via Docker
docker compose up                         # All services (api :8000, frontend :3000, mcp :8100)

# Tests
uv run pytest                             # All backend tests
uv run pytest tests/test_auth.py          # Single test file
uv run pytest -k test_name                # Single test by name
cd frontend && bun run lint               # Frontend ESLint

# Linting
uv run ruff check .                       # Python lint
uv run ruff format .                      # Python format
```

## Architecture

- **Backend**: Django 5 + DRF in `config/` and `apps/` (ASGI via uvicorn, async-first)
- **Frontend**: React 19 + Vite + Tailwind CSS 4 + TypeScript in `frontend/`
- **AI**: LangGraph agent with langchain-anthropic, PostgreSQL checkpointer for conversation persistence
- **MCP Server**: Standalone FastMCP server (`mcp_server/`) for tool-based data access (SQL execution, table metadata)
- **Auth**: Session cookies (no JWT), CSRF token from `GET /api/auth/csrf/`
- **DB encryption**: Project database credentials encrypted with Fernet (`DB_CREDENTIAL_KEY` env var)

### Django apps (`apps/`)

| App | Purpose |
|-----|---------|
| users | Custom User model, session auth, OAuth (Google/GitHub/CommCare) |
| workspaces | Workspaces, DB connections (encrypted), memberships |
| knowledge | KnowledgeEntry, table metadata, golden queries, eval runs |
| agents | LangGraph agent graph, MCP client, tools, prompts, memory (checkpointer) |
| chat | Streaming chat threads with LangGraph agent |
| artifacts | Generated dashboards/charts with sandboxed React rendering |
| recipes | Replayable analysis workflows with templated prompts |

### Settings modules (`config/settings/`)

- `base.py` - Shared config (apps, middleware, auth, REST framework)
- `development.py` - DEBUG=True, console email
- `production.py` - HTTPS enforced, secure cookies, HSTS
- `test.py` - Test DB, MD5 hasher, in-memory email

## Async conventions

The codebase is async-first. New views should be `async def` using native Django async ORM.

- **Views**: Raw `async def` views for streaming/chat/tenant endpoints; DRF `APIView` stays sync (DRF doesn't support async streaming)
- **ORM**: Use async methods (`.aget()`, `.afirst()`, `.afilter()`, `.acreate()`, `.aupdate_or_create()`, `async for`) — never call sync ORM from async views
- **Auth**: Use `await request.auser()` (Django 5 native), not `sync_to_async(get_user)`
- **DB connections**: `AsyncConnectionPool` (psycopg_pool) for LangGraph checkpointer; `psycopg.AsyncConnection.connect()` for direct queries in MCP server
- **`sync_to_async`**: Only for wrapping external API calls (OAuth token refresh, CommCare API) and inherently sync operations (dbt). Do not use for ORM calls.

## Environment variables

Required (see `.env.example`):
- `DATABASE_URL` - Platform PostgreSQL connection string
- `ANTHROPIC_API_KEY` - Claude API key for LangGraph agent
- `DB_CREDENTIAL_KEY` - Fernet key for encrypting project DB credentials
- `DJANGO_SECRET_KEY` - Django secret key

Optional:
- `MCP_SERVER_URL` - MCP server URL (default: `http://localhost:8100/mcp`)
- `REDIS_URL` - Redis connection URL for caching and Celery

## Code style

- **Python**: ruff (line-length=100, target py311, rules: E/F/I/UP/B)
- **Imports**: Always at module level, never inside function bodies. Exceptions: optional dependencies guarded by `try/except ImportError`, and code that must run before `django.setup()`. When moving inline imports to module level, update any `mock.patch()` targets in tests to point at the consuming module (where the name is used), not the source module.
- **Frontend**: ESLint with typescript-eslint + react-hooks plugin
- **No Prettier** configured for frontend

## Testing conventions

### data-testid attributes

Interactive UI elements that QA automation (showboat/rodney) targets must have `data-testid` attributes. This decouples tests from CSS classes and DOM structure so styling changes don't break test scenarios.

Naming convention: `{component}-{element}` using kebab-case. Dynamic names use the pattern `{component}-{identifier}`, e.g. `table-item-users`, `schema-group-public`, `column-note-email`.

When adding new interactive elements to pages that have QA scenarios in `tests/qa/`, add a `data-testid` to any element a test might need to click, read, or assert on.

### Async tests

Tests for async views use `pytest-asyncio` with Django's `AsyncClient`:

```python
@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_my_async_view(user):
    client = AsyncClient()
    await sync_to_async(client.login)(email=user.email, password="pass")
    response = await client.post("/api/chat/", ...)
```

- Async DB tests require `transaction=True` (not just `@pytest.mark.django_db`)
- Fixtures stay sync (standard pytest-django); async tests fetch via async ORM if needed
