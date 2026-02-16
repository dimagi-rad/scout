# Scout

Self-hosted data agent platform for AI-powered database querying.

## Commands

```bash
# Backend
docker compose up platform-db redis      # Start dependencies
uv run python manage.py runserver         # Django dev server (or use uvicorn below)
uv run uvicorn config.asgi:application --reload --port 8000  # ASGI dev server
uv run python manage.py migrate           # Run migrations

# Frontend
cd frontend && bun install && bun dev     # Dev server on :5173
cd frontend && bun run build              # Production build (runs tsc first)

# Full stack via Docker
docker compose up                         # All services (api :8000, frontend :3000)

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

- **Backend**: Django 5 + DRF in `config/` and `apps/` (ASGI via uvicorn)
- **Frontend**: React 19 + Vite + Tailwind CSS 4 + TypeScript in `frontend/`
- **AI**: LangGraph agent with langchain-anthropic, PostgreSQL checkpointer for conversation persistence
- **Auth**: Session cookies (no JWT), CSRF token from `GET /api/auth/csrf/`
- **DB encryption**: Project database credentials encrypted with Fernet (`DB_CREDENTIAL_KEY` env var)

### Django apps (`apps/`)

| App | Purpose |
|-----|---------|
| users | Custom User model, session auth, OAuth (Google/GitHub/CommCare) |
| projects | Projects, DB connections (encrypted), memberships |
| knowledge | KnowledgeEntry, table metadata, golden queries, eval runs |
| chat | Streaming chat threads with LangGraph agent |
| artifacts | Generated dashboards/charts with sandboxed React rendering |
| recipes | Replayable analysis workflows with templated prompts |

### Settings modules (`config/settings/`)

- `base.py` - Shared config (apps, middleware, auth, REST framework)
- `development.py` - DEBUG=True, console email
- `production.py` - HTTPS enforced, secure cookies, HSTS
- `test.py` - Test DB, MD5 hasher, in-memory email

## Environment variables

Required (see `.env.example`):
- `DATABASE_URL` - Platform PostgreSQL connection string
- `ANTHROPIC_API_KEY` - Claude API key for LangGraph agent
- `DB_CREDENTIAL_KEY` - Fernet key for encrypting project DB credentials
- `DJANGO_SECRET_KEY` - Django secret key

## Code style

- **Python**: ruff (line-length=100, target py311, rules: E/F/I/UP/B)
- **Frontend**: ESLint with typescript-eslint + react-hooks plugin
- **No Prettier** configured for frontend

## Testing conventions

### data-testid attributes

Interactive UI elements that QA automation (showboat/rodney) targets must have `data-testid` attributes. This decouples tests from CSS classes and DOM structure so styling changes don't break test scenarios.

Naming convention: `{component}-{element}` using kebab-case. Dynamic names use the pattern `{component}-{identifier}`, e.g. `table-item-users`, `schema-group-public`, `column-note-email`.

When adding new interactive elements to pages that have QA scenarios in `tests/qa/`, add a `data-testid` to any element a test might need to click, read, or assert on.
