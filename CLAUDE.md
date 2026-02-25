# Scout

Self-hosted data agent platform for AI-powered database querying.

## Commands

```bash
# Backend
docker compose up platform-db redis mcp-server  # Start dependencies
uv run python manage.py runserver         # Django dev server (or use uvicorn below)
uv run uvicorn config.asgi:application --reload --port 8000  # ASGI dev server
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

- **Backend**: Django 5 + DRF in `config/` and `apps/` (ASGI via uvicorn)
- **Frontend**: React 19 + Vite + Tailwind CSS 4 + TypeScript in `frontend/`
- **AI**: LangGraph agent with langchain-anthropic, PostgreSQL checkpointer for conversation persistence
- **MCP Server**: Standalone FastMCP server (`mcp_server/`) for tool-based data access (SQL execution, table metadata)
- **Auth**: Session cookies (no JWT), CSRF token from `GET /api/auth/csrf/`
- **DB encryption**: Project database credentials encrypted with Fernet (`DB_CREDENTIAL_KEY` env var)

### Django apps (`apps/`)

| App | Purpose |
|-----|---------|
| users | Custom User model, session auth, OAuth (Google/GitHub/CommCare) |
| projects | Projects, DB connections (encrypted), memberships |
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

## Environment variables

Required (see `.env.example`):
- `DATABASE_URL` - Platform PostgreSQL connection string
- `ANTHROPIC_API_KEY` - Claude API key for LangGraph agent
- `DB_CREDENTIAL_KEY` - Fernet key for encrypting project DB credentials
- `DJANGO_SECRET_KEY` - Django secret key

Optional:
- `MCP_SERVER_URL` - MCP server URL (default: `http://localhost:8100/mcp`)
- `REDIS_URL` - Redis connection URL for caching and Celery

## Working in Git worktrees

Multiple agents can work simultaneously in isolated worktrees under `.worktrees/`. The steps below are required every time you create a new worktree — none of these files carry over automatically.

### Setup checklist

**Copy `.env`** — it is gitignored and not shared. From the worktree root:

```bash
cp $(git rev-parse --show-toplevel)/.env .
```

**Install frontend dependencies** — `node_modules` is also gitignored. Use `bun install` rather than copying the directory: bun deduplicates packages via its global cache so it's fast and doesn't waste disk space.

```bash
cd frontend && bun install
```

**Start servers on alternate ports** — other agents may already hold ports 8000 (backend), 5173 (frontend), and 8100 (MCP). Check first, then start on free ports. Use a branch-specific log prefix in `/tmp` to avoid collisions:

```bash
lsof -i :8000,5173,8100 -sTCP:LISTEN

BRANCH=$(git branch --show-current | tr '/' '-')

# Backend — source .env so all vars are loaded
source .env && DJANGO_SETTINGS_MODULE=config.settings.development \
  uv run uvicorn config.asgi:application --reload --port 8002 \
  > /tmp/${BRANCH}-web.log 2>&1 &

# Frontend — API_PORT tells Vite's proxy which backend to forward to
cd frontend && API_PORT=8002 bun run vite --port 5175 \
  > /tmp/${BRANCH}-frontend.log 2>&1 &
```

**Do not use `honcho` in worktrees.** It also starts MCP, which requires additional env config and always targets the default ports, causing collisions.

**Trust the frontend port for CSRF** — Django rejects POST requests from unrecognised origins. Add your port to the worktree's `.env` before starting the backend (no code changes needed; `CSRF_TRUSTED_ORIGINS` is read from the environment):

```bash
echo 'CSRF_TRUSTED_ORIGINS=http://localhost:5173,http://localhost:5175' >> .env
```

**`API_PORT` controls the Vite proxy target** — `frontend/vite.config.ts` reads `API_PORT` from the project-root `.env`. Pass it inline when launching Vite so the proxy forwards to your backend port rather than the default 8000.

## UI verification with Playwright CLI

Use `playwright-cli` to reproduce and verify UI behaviour. It writes screenshots and snapshot `.yml` files to whichever directory the command is run from. Use a branch-specific subdirectory under `/tmp` to keep everything out of the repo and avoid conflicts between concurrent agents:

```bash
BRANCH=$(git branch --show-current | tr '/' '-')
mkdir -p /tmp/${BRANCH}-playwright
cd /tmp/${BRANCH}-playwright && playwright-cli open http://localhost:5175
```

Screenshots saved with `--filename` also resolve relative to the working directory, so they'll land in `/tmp/${BRANCH}-playwright/` automatically.

### Login flow

The frontend fetches `/api/auth/csrf/` automatically before login — no manual setup needed. The basic sequence:

```bash
playwright-cli open http://localhost:5175
playwright-cli snapshot                        # get element refs
playwright-cli fill <email-ref> "user@example.com"
playwright-cli fill <pwd-ref> "password"
playwright-cli click <submit-ref>
playwright-cli screenshot --filename=after-login.png
```

Refs are reassigned after every navigation — always take a fresh snapshot before interacting with new elements.

## Code style

- **Python**: ruff (line-length=100, target py311, rules: E/F/I/UP/B)
- **Frontend**: ESLint with typescript-eslint + react-hooks plugin
- **No Prettier** configured for frontend

## Testing conventions

### data-testid attributes

Interactive UI elements that QA automation (showboat/rodney) targets must have `data-testid` attributes. This decouples tests from CSS classes and DOM structure so styling changes don't break test scenarios.

Naming convention: `{component}-{element}` using kebab-case. Dynamic names use the pattern `{component}-{identifier}`, e.g. `table-item-users`, `schema-group-public`, `column-note-email`.

When adding new interactive elements to pages that have QA scenarios in `tests/qa/`, add a `data-testid` to any element a test might need to click, read, or assert on.
