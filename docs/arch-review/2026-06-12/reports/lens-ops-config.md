# Lens review: Ops / Config / Deployment

*Reviewer: cross-cutting lens "ops-config" (arch review v2, 2026-06-12).*
*Scope: settings-module drift, env-var handling, docker-compose vs production parity, in-flight jobs on deploy/restart, migration discipline, secrets in logs/images.*
*Report only — no code changed. Evidence standards per docs/arch-review-methodology.md.*

## Environment map (as deployed, verified from repo)

Production = one EC2 (t3.medium) running 4 Kamal services from 2 images:

| Service | Kamal config | cmd | Image |
|---|---|---|---|
| API | `config/deploy.yml` | uvicorn, `--workers 4` | scout/api |
| MCP | `config/deploy-mcp.yml` | `python -m mcp_server` | scout/api |
| Worker | `config/deploy-worker.yml` | `python manage.py procrastinate worker` (no flags) | scout/api |
| Frontend | `config/deploy-frontend.yml` | nginx (sole public entry, kamal-proxy TLS) | scout/frontend |

Plus: RDS db.t4g.micro Postgres 16 (`infra/scout-stack.yml:16`), ElastiCache Redis cache.t4g.micro (`:21`), CloudWatch awslogs (30-day retention). A second environment ("connect-labs", ECS Fargate) is deployed by `.github/workflows/deploy-labs.yml` with `config/settings/connectlabs.py`; its task definitions live outside this repo.

`.github/workflows/deploy.yml` deploys **on every push to main**, in order: build images → MCP → API → Worker → Frontend. Migrations run inside the API container at boot (`docker-entrypoint.sh`, gated on `$1 == uvicorn`).

---

## Findings

### F1. Every merge to main can kill an in-flight materialization, and the killed job is never rescued (BROKEN-NOW · correctness · verified-by-trace)

This is the live sibling of the 2026-05-30 "zombie `doing` jobs" incident, and it re-arms on every deploy.

Chain:

1. **Entry**: `.github/workflows/deploy.yml` — `on: push: branches: [main]` → step "Deploy Worker": `kamal deploy -c config/deploy-worker.yml`. Fires on every merged PR.
2. `config/deploy-worker.yml` configures **no `stop_wait_time`** (full file read). Kamal replaces the container: SIGTERM, then docker's kill after the default grace period (seconds, not minutes).
3. Procrastinate 3.8.1 worker (`uv.lock:2003`, version verified at 3.8.1): `Worker.__init__` default `shutdown_graceful_timeout: float | None = None` — on SIGTERM it waits for the running job indefinitely; a materialization (tens of minutes) is therefore guaranteed to still be running when docker SIGKILLs.
4. After SIGKILL the job row stays `status='doing'`. Procrastinate's own recovery is opt-in: the worker only calls `prune_stalled_workers` at startup, and the SQL (`procrastinate/sql/schema.sql`, `procrastinate_prune_stalled_workers_v1`) **only `DELETE FROM procrastinate_workers`** — it never requeues the dead worker's jobs. Rescue requires the app to call `job_manager.get_stalled_jobs()` + `retry_job()`; `grep -rn "stalled" apps/ config/ mcp_server/` → zero hits. Scout never calls it.
5. Scout's own janitors deliberately skip these jobs: `apps/workspaces/tasks.py:749-750` in `reconcile_stale_thread_job`:
   ```python
   if status in {"todo", "doing"}:
       return None
   ```
   Both the worker-side janitor (`expire_stale_thread_jobs`, tasks.py:819) and the API-side backstop poll share this function (docstring at tasks.py:730-739), so neither ever touches a job stuck in `doing`.
6. **Consequence**: `MaterializationRun` stays STARTED, `ThreadJob` stays active, UI shows progress forever. The only recovery is the manual orphan-cancel path added 2026-05-30 (`ceddcd0`, `301d373`, `4b438d4`) — i.e., the user clicks cancel and retries; partial-page work is preserved only insofar as cursor watermarks (#187) cover that table.

Reachable via: any push to main while any workspace is materializing. Deploy frequency (multiple merges/day in busy weeks per churn stats) × materialization duration makes the overlap routine, and the identical failure already occurred in production (2026-05-30 incident, worker death rather than deploy).

Complexity: **accidental** — upstream provides both halves of the fix (a `shutdown_graceful_timeout`/`stop_wait_time` long enough for graceful drain, and `get_stalled_jobs`/`retry_job` for a janitor sweep); neither is wired.

Note the asymmetry: the janitor's "don't touch `doing`" rule is *correct* for live jobs and was a deliberate post-incident choice (tasks.py:708-712 docstring), but with worker heartbeats now available in procrastinate 3.8 the janitor could distinguish "doing on a live worker" from "doing with no worker heartbeat" and stop being blind to exactly the case it was built for.

### F2. Production background capacity is one process × concurrency 1 × one queue (DEBT · cost-perf · verified-by-trace)

- `config/deploy-worker.yml` cmd: `python manage.py procrastinate worker` — no `--concurrency`, no queues.
- Procrastinate 3.8.1 default: `concurrency: int = 1` (verified from `Worker.__init__` signature).
- No task in `apps/workspaces/tasks.py` declares a queue (grep `queue=` → none), so everything — every workspace's materialization, schema teardown, view-schema rebuilds, thread resumes, and both periodic janitors (`*/15`, `*/30` crons) — serializes through a single job slot.

Consequences: two users materializing concurrently queue behind each other; while a long materialization runs, the TTL janitor and ThreadJob janitor **cannot run at all** (their deferred periodic jobs wait for the slot). The API-side reconcile backstop (tasks.py:730-739) exists precisely because the worker can be unavailable, but it only covers ThreadJobs, not `expire_inactive_schemas`. Single worker container is also a SPOF with no liveness probe (see F10).

Complexity: accidental — `--concurrency N` is a one-line change (subject to the materializer's own concurrency-safety, which this lens did not audit).

### F3. No `.dockerignore`: the documented manual-deploy path bakes the developer's working tree — including `.env` — into the production image, and `base.py` reads it at runtime (LATENT · security · verified-by-trace)

Chain:

1. No `.dockerignore` exists (checked; `cat` fails). `Dockerfile` does `COPY . .` into `/app`.
2. `DEPLOYMENT.md:149-195` documents manual deploys from a developer machine (`kamal setup`, `kamal deploy`) as a supported path; Kamal builds locally in that flow. A dev checkout contains `.env` (real ANTHROPIC key, Fernet key, OAuth secrets per `.env.example` contract), `.env.deploy` (RDS endpoint, secret ARN, deploy role ARN), `.git`, `frontend/node_modules`, worktrees, test artifacts — all copied into image layers and pushed to ECR.
3. `config/settings/base.py:24-26`:
   ```python
   env_file = BASE_DIR / ".env"
   if env_file.exists():
       env.read_env(str(env_file))
   ```
   runs in every container. `read_env` does not overwrite existing env vars, so Kamal-injected secrets win — but **any var the Kamal configs don't set is silently sourced from the baked dev `.env`**: e.g. `SOCIALACCOUNT_ALLOWED_EMAIL_DOMAINS`, `DEFAULT_LLM_MODEL`, `CONNECT_API_URL`, `OCS_URL`, `AGENT_RESUME_*` are absent from all three backend deploy configs (read in full). Production behavior then depends on whose laptop built the image.

CI builds (`deploy.yml`, fresh checkout) are clean — this is specifically the manual path, which DEPLOYMENT.md positions for "debugging or first-time setup", i.e. exactly the high-stress moments. Secondary effect even on CI builds: `.git`, `tests/`, `docs/` in the image (bloat, slower pushes from a t3.medium-served registry path).

Complexity: accidental. One `.dockerignore` plus (ideally) dropping the implicit `.env` read under production settings.

### F4. Deploys are not gated on tests, and migration ordering is by convention only (DEBT · correctness · verified-by-trace)

- `.github/workflows/ci.yml` runs **only on `pull_request`**. `.github/workflows/deploy.yml` runs on `push` to main with **no dependency on CI**: a direct push, a merge of two individually-green-but-jointly-broken PRs, or an admin-merge with red CI deploys straight to production. The only gates are the image builds themselves (frontend build does run `tsc`).
- Migration ordering: migrations run at **API container boot** (`docker-entrypoint.sh:7-9`), but the deploy order is **MCP → API → Worker** (`deploy.yml` steps). So new MCP code serves traffic against the old schema until the API container has booted and migrated; the old worker runs against the new schema until step 3. Both windows are real but short; nothing enforces backward-compatible migrations, and no doc states the expand/contract discipline. A mid-pipeline failure (e.g. "Deploy API" fails after "Deploy MCP" succeeded) leaves mixed versions with no documented rollback.
- CI has no `manage.py makemigrations --check` step (verified: ci.yml contains only ruff / eslint / pytest), so a model change without its migration reaches the deploy pipeline; it would typically surface as test failures only if a touched test exercises the column.
- `DEPLOYMENT.md:35` claims "Runs migrations in a pre-deploy hook (API service only)" — but `.kamal/hooks/pre-deploy` is `exit 0` with a comment explaining migrations deliberately moved to the entrypoint. Doc/code mismatch (the doc describes a mechanism that was removed).

### F5. Labs environment: migrations are opt-in-per-deploy and default off (LATENT · correctness · verified-by-trace for the mechanism)

`.github/workflows/deploy-labs.yml`: `run_migrations` is a manual checkbox, `default: false`. Deploying a backend image whose models require new columns, with the box unchecked, ships code against an unmigrated DB — failing at runtime, not boot. The auto-detect logic ensures the *image* is fresh but nothing detects pending migrations. The ECS task definitions (env vars, settings module wiring) live outside this repo, so labs/production parity is unauditable from here.

### F6. `DEPLOY_ENVIRONMENT` detection breaks for the connectlabs settings module (LATENT · velocity · strong-inference)

`config/settings/base.py:41-45`:
```python
DEPLOY_ENVIRONMENT = (
    "production"
    if os.environ.get("DJANGO_SETTINGS_MODULE", "").endswith(".production")
    else "development"
)
```
`config/settings/connectlabs.py` inherits all of production's posture (`from .production import *`) but its module path ends in `.connectlabs` → Sentry and Task Badger default to environment="development" for the labs deployment unless every ECS task def explicitly sets `SENTRY_ENVIRONMENT`/`TASKBADGER_ENVIRONMENT` (unverifiable from the repo — task defs are external). The Kamal production configs do set `SENTRY_ENVIRONMENT: "production"` explicitly, which is itself evidence the default isn't trusted. The comment block above the code (base.py:36-40) carefully explains why the heuristic exists but doesn't account for the third settings module sitting two files away. Comment-vs-reality drift.

### F7. Redis is provisioned, secured, paid for — and has zero consumers; meanwhile production cache is per-process LocMem so rate limiting is ~4× advertised (DEBT · cost-perf · verified-by-trace)

- `infra/scout-stack.yml:283-296` provisions `scout-redis` (ElastiCache 7.1, cache.t4g.micro), with its own security group, subnet group, and a stack output; `scripts/fetch-deploy-env.sh` exports `SCOUT_REDIS_ENDPOINT`; `deploy.yml` workflow threads it through GitHub secrets.
- `grep -rni redis apps/ config/ mcp_server/ pyproject.toml` → **zero hits**. No Python Redis client is even installed. Vestige of the Celery era (Celery→Procrastinate migration 2026-05-01); `DEPLOYMENT.md:10` still says "ElastiCache Redis 7 — caching and Celery broker" and `:23` still labels the worker "Celery".
- Meanwhile `config/settings/base.py:318-325` ships `LocMemCache` with the note "rate limiting won't work across multiple workers. Set up a shared cache for production deployments" — and `config/settings/production.py` (read in full) does not override `CACHES`. The API runs `--workers 4` (`deploy.yml` cmd), so DRF throttles (`base.py:263-271`, anon 60/min, user 120/min) are per-process: effective limits are ~4× the configured numbers, jittery, and reset on every deploy.

The acknowledged-in-comment gap and the idle infrastructure that would close it have coexisted for ~6 weeks.

### F8. Platform-DB connection budget vs db.t4g.micro (LATENT · cost-perf · strong-inference)

`apps/chat/checkpointer.py:29-38` opens an `AsyncConnectionPool(max_size=20)` per process; the API runs 4 uvicorn worker processes → up to 80 checkpointer connections, plus per-process Django ORM connections (no `CONN_MAX_AGE` tuning anywhere in settings), plus the worker (ORM + procrastinate LISTEN/NOTIFY + its own checkpointer pool when resume tasks run), plus MCP (ORM + direct psycopg per query). RDS `db.t4g.micro` (1 GiB, `infra/scout-stack.yml:16`) defaults to `max_connections` ≈ 110. Headroom exists only because traffic is low; a burst of chat threads across 4 workers can exhaust connections platform-wide (symptom would be "checkpointer failed in production" 500s — `apps/chat/checkpointer.py:50-55` correctly fails closed). No incident is on record for this; flagged as arithmetic, not observation.

Related migration-discipline note: `AsyncPostgresSaver.setup()` runs at first use in **every** process (`chat/checkpointer.py:41`) — LangGraph checkpoint DDL is applied at runtime by whichever container touches it first, outside Django migrations, with no version pinning between the three processes that do this.

### F9. Checkpointer DB-config resolution bypasses Django settings, and a dead module carries a dangerous silent fallback (DEBT · correctness · verified-by-trace)

- `apps/agents/memory/checkpointer.py:43-59`: resolves the platform DB as raw `os.environ["DATABASE_URL"]` first, then `DB_HOST/DB_NAME/...` (a five-var convention used nowhere else in the repo, not in `.env.example`, not in any deploy config), then Django settings. Two sources of truth for "the platform DB": Django's `DATABASES` (via django-environ, possibly assembled from `DATABASE_USER/...` parts as in CI/test) and this env-first path. Today they agree everywhere that matters; any future env where they diverge (PgBouncer for ORM only, read replica, CI quirk) splits chat history from the rest of the platform silently.
- Same module, `get_postgres_checkpointer` (line 90) **falls back to `MemorySaver` on any connection failure** — in production this would silently drop conversation persistence per-process. Mitigation: it appears to have no callers (`grep -rn get_postgres_checkpointer apps/` → definition + its own docstring only); the live chat path `apps/chat/checkpointer.py:43-55` correctly raises in production and only falls back under `settings.DEBUG`. The dead module is a loaded footgun for the next caller (overlaps the dead-code lens; recorded here because the failure class is "prod silently degrades durability").

### F10. No health gating for API/MCP/Worker deploys; `/health/` exists but nothing in production uses it (DEBT · velocity · verified-by-trace)

All three backend Kamal configs set `proxy: false` and define no healthcheck (files read in full); only the frontend has a kamal-proxy healthcheck, and it checks `/` on nginx — which serves the SPA fine even when the API is dead. `docker-compose.yml` is the only place `/health/` is polled. Consequence: a deploy that boots a crash-looping API/MCP/worker container is reported green by the pipeline; detection is via users or Sentry. The 2026-06-09 incident (worker dead 22h) is the lived version of "the worker has no liveness signal"; the connection-hygiene decorator fixed that failure mode, not the observability gap (overlaps the observability lens).

Adjacent inconsistency: `frontend/nginx.prod.conf` (labs) proxies `/scout/widget.js` to Django, but `frontend/nginx.prod-kamal.conf` (scout.dimagi.com) has **no `/widget.js` location** — it falls through to the SPA catch-all and serves `index.html`. So the embed widget SDK is unreachable on the primary production host while the backend route (`config/urls.py` → `widget_js_view`) remains live. Either dead surface (remove route) or broken surface (add location) — currently it is silently both, differently, per environment.

### F11. `SOCIALACCOUNT_ALLOWED_EMAIL_DOMAINS`: documented default ≠ coded default, and production runs the coded one (LATENT · security · verified-by-trace for the mismatch)

- `.env.example:38-41`: "Defaults to [\"dimagi.com\"] for each of commcare, commcare_connect, and ocs if unset."
- `config/settings/base.py:247-252`: the actual default restricts **only** `"commcare"`; `commcare_connect` and `ocs` are unrestricted ("A provider absent from the dict ... is unrestricted", base.py:242).
- `config/deploy.yml` does not set the env var (full env block read), so production runs the code default: Connect and OCS OAuth sign-ins are open to any email domain (and Connect logins with no email bypass the check by design, base.py:244-245).

Whether the open default is intended is a product question; the doc/code contradiction is not. Per evidence standards, the comment is the claim and the code is the fact — one of them is wrong. (AuthZ implications belong to the security lens; recorded here as config-contract drift.)

### F12. Server entrypoints default to development settings; only MCP fails fast (LATENT · security · verified-by-trace)

`config/asgi.py:14`, `config/wsgi.py:14`, `manage.py:10` all `setdefault("DJANGO_SETTINGS_MODULE", "config.settings.development")`, and `base.py:34` defaults `DJANGO_DEBUG=True`. A production container that loses its `DJANGO_SETTINGS_MODULE` (new deploy config, ECS task-def typo in labs, ad-hoc `docker run` for debugging) silently boots with DEBUG=True, permissive CSRF origins, and dev logging — it does not crash, because every secret it needs is injected anyway. Contrast `mcp_server/__main__.py:5-9`, which raises `RuntimeError` when the variable is missing: the codebase already contains the right pattern, applied to one process out of three. Today's deploy configs all set the variable correctly (verified in all four Kamal files + compose), hence LATENT.

### F13. Capability tokens and OAuth codes transit access logs (LATENT · security · strong-inference)

Public share endpoints put the bearer capability in the URL path (`/api/chat/threads/shared/<share_token>/`, `/api/recipes/runs/shared/<share_token>/` — `config/urls.py` per cartography; routes confirmed live in v1 reviews), and OAuth callbacks carry `?code=...`. Uvicorn's access log is on by default and is not configured off (`deploy.yml` cmd has no `--no-access-log`; Django's LOGGING doesn't govern uvicorn's own loggers), and the frontend nginx also logs request lines. Both ship to CloudWatch with 30-day retention (`infra/scout-stack.yml:369-387`). Anyone with CloudWatch read access can harvest live share tokens. Not verified against actual prod log output (no runtime access) — hence strong-inference, not trace. Share tokens are the real exposure; OAuth codes are single-use/short-lived.

### F14. MCP container's env set is a third, smaller dialect — Task Badger/Langfuse absent at a deferral site (LATENT · velocity · strong-inference)

`config/deploy-mcp.yml` passes neither `TASKBADGER_API_KEY` nor Langfuse keys. But MCP is a procrastinate **deferral site**: `mcp_server/server.py:607` defers `materialize_workspace` directly. `config/deploy-worker.yml`'s own comments establish that deferring creates Task Badger records ("it also chains the resume task, so it defers (creates) Task Badger records too"). By that documented model, materializations launched through the agent/MCP path (the primary path) are invisible to Task Badger, while API- or worker-deferred tasks are tracked — exactly the kind of partial observability that prolonged the 2026-06-09 diagnosis. Not traced into the taskbadger SDK internals (defer-time vs execution-time record creation), hence strong-inference. The three backend env blocks (`deploy.yml` / `deploy-worker.yml` / `deploy-mcp.yml`) are maintained by hand with per-file comments explaining past omissions that bit ("Without this the worker falls back to localhost:8100 and every resume task fails") — the pattern of discovering missing env vars in production one outage at a time is structural: there is no shared env manifest.

### F15. docker-compose "full stack" has no worker — background processing can't run at all in the compose stack (DEBT · velocity · verified-by-trace)

`docker-compose.yml` defines `platform-db`, `mcp-server`, `api`, `frontend` (full file read). No procrastinate worker service. `CLAUDE.md` advertises `docker compose up` as "All services". Any flow involving materialization, schema teardown, janitors, or thread resume silently queues forever in the compose stack (jobs defer fine; nothing executes them). Honcho's `Procfile.dev` does include a worker, so the gap is compose-specific — which is also the configuration closest to production topology, i.e. the one you'd reach for to reproduce a prod incident.

### F16. Documentation drift inventory (COSMETIC · velocity · verified-by-trace)

`DEPLOYMENT.md`: ":10 Redis = 'caching and Celery broker'" (no Redis consumer, no Celery); ":23 Worker (Celery)" (Procrastinate); ":26 nginx proxies '/api/ and /mcp/'" (no `/mcp/` location in either prod nginx conf — good, but the doc describes an exposure that would be a security bug if true); ":35 migrations 'in a pre-deploy hook'" (hook is `exit 0`; migrations are in the entrypoint); ":11 ECR registry '(scout/api, scout/mcp, scout/frontend)'" (no scout/mcp image — MCP runs scout/api). Also `.kamal/hooks/pre-connect` hardcodes `-i ~/.ssh/scout-deploy.pem` with `IdentitiesOnly=yes`, while DEPLOYMENT.md:158-163 recommends the 1Password SSH agent — a manual deploy following the doc's recommended setup fails at the pre-connect hook unless the pem also exists on disk. Each item is small; collectively the deploy doc cannot be followed literally.

---

## What's fine (verified)

- **Secrets pipeline**: AWS Secrets Manager → `.kamal/secrets` → `env.secret` is clean and consistently used; no secret values committed; the RDS password is AWS-managed and URL-encoded properly (`scripts/resolve-database-url.sh`); CI auth is OIDC (no long-lived keys); the Sentry source-map token uses a BuildKit secret and never lands in a layer (`deploy.yml` workflow + `DEPLOYMENT.md:143-147`).
- **No secret values in log statements**: grepped all `logger.*` calls mentioning token/password/secret/key/credential — messages log IDs and client_ids, never values.
- **Task args carry IDs only**: `materialize_workspace(workspace_id, user_id)`, `resume_thread_after_materialization(thread_job_id)` — so procrastinate's job table and Task Badger's `record_task_args=True` (`config/taskbadger.py`) don't persist OAuth tokens; the resume task loads tokens inside the task body (tasks.py:853-867).
- **django-environ bool casting**: verified empirically that `env("SECURE_SSL_REDIRECT", default=True)` smart-casts `"False"` → `False`; the `deploy.yml` `SECURE_SSL_REDIRECT: "False"` override works as intended.
- **Worker connection-hygiene decorator** (`config/procrastinate.py`): well-commented, explicitly temporary with upstream issue links, and enforced by a test (`tests/test_worker_db_resilience.py` requires every task to be registered through it). Exemplary handling of a stopgap.
- **Chat checkpointer fails closed in production** (`apps/chat/checkpointer.py:43-55`): MemorySaver fallback is DEBUG-gated.
- **Production security headers** (`config/settings/production.py`): HSTS, secure cookies, nosniff, SAMESITE=None only when embedding is configured and only with Secure=True. Coherent.
- **Single-migrator discipline**: only the API entrypoint migrates (`docker-entrypoint.sh` gates on `$1 == uvicorn`), so MCP/worker boots can't race migrations against each other.
- **MCP is not publicly exposed**: neither prod nginx conf proxies it; it's reachable only on the `scout_shared` docker network.
- **CloudWatch logging**: per-service groups, 30-day retention defined in CFN, with a documented create-group-before-deploy ordering caveat and a rollback procedure.

## Coverage log

**Deep-read (line-by-line):** `config/settings/base.py`, `production.py`, `development.py`, `test.py`, `connectlabs.py`; `config/deploy.yml`, `deploy-worker.yml`, `deploy-mcp.yml`, `deploy-frontend.yml`; `.kamal/secrets`, `.kamal/hooks/pre-connect`, `.kamal/hooks/pre-deploy`; `Dockerfile`, `docker-entrypoint.sh`, `docker-compose.yml`, `docker-compose.override.yml`, `Procfile.dev`, `.env.example`; `scripts/fetch-deploy-env.sh`, `scripts/resolve-database-url.sh`, `registry_password.sh`; `.github/workflows/deploy.yml`, `deploy-labs.yml`, `ci.yml`; `config/procrastinate.py`, `config/taskbadger.py`; `manage.py`, `config/asgi.py`, `config/wsgi.py`; `DEPLOYMENT.md`; `apps/agents/memory/checkpointer.py`, `apps/chat/checkpointer.py`; `apps/workspaces/tasks.py` lines 203-235 and 690-840 (task signatures, janitor/reconcile machinery); `mcp_server/__main__.py`.

**Skimmed (targeted grep/section reads):** `infra/scout-stack.yml` (instance classes, Redis, RDS, log groups — not IAM/SG/userdata), `frontend/nginx.prod.conf` + `nginx.prod-kamal.conf` (location blocks only), `mcp_server/server.py` (defer site, main()), `apps/users/management/commands/setup_oauth_apps.py` (env-var reads), `pyproject.toml`/`uv.lock` (procrastinate pin), procrastinate 3.8.1 installed sources (Worker signature, prune/stalled SQL, JobManager API), `apps/workspaces/tasks.py` remainder (structure grep only).

**Not examined:** `Dockerfile.frontend` and `frontend/nginx.conf` (dev) in detail; `config/middleware/embed.py`, `config/urls.py`, `config/views.py` (widget_js_view internals); `.github/workflows/claude.yml`, `docs.yml`; `infra/scout-stack.yml` IAM roles, security-group rules, EC2 user-data; ECS task definitions for connect-labs (out of repo — labs env parity is unauditable); any runtime/production verification (no access — F8, F13, F14 carry that caveat); Kamal's exact `stop_wait_time` default for the installed version (F1 step 2 is therefore "seconds-scale grace", not a quoted number); the materializer/loader env handling inside `mcp_server/services/`; `apps/agents/tracing.py` (Langfuse wiring); `apps/chat/rate_limiting.py` internals; `tests/` except confirming `test_worker_db_resilience.py` exists; `templates/`; management commands other than `setup_oauth_apps`; frontend build config (`vite.config.ts`).
