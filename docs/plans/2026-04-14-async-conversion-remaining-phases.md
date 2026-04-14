# Async Conversion — Remaining Phases

> Continuation of Phase 1 (completed in PR #145). Documents the remaining
> `sync_to_async` / `async_to_sync` call sites and the plan to eliminate them.

**Current state after Phase 1:** 22 call sites removed. ~20 remain across 5 files.

---

## Phase 2: External HTTP — replace `requests` with `httpx`

**Goal:** Replace the synchronous `requests` library with `httpx.AsyncClient` in
all external API call sites so they run natively on the event loop instead of
dispatching to a thread pool.

**Dependency:** Add `httpx` to `pyproject.toml`.

### 2a. Tenant verification (`apps/users/services/tenant_verification.py`)

**Current:** `verify_commcare_credential` uses `requests.get()` to call the
CommCare user_domains API. Callers in `apps/users/views.py:172,256` wrap it
with `sync_to_async`.

**Convert to:**

```python
import httpx

async def verify_commcare_credential(domain: str, username: str, api_key: str) -> dict:
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{COMMCARE_API_BASE}/api/user_domains/v1/",
            headers={"Authorization": f"ApiKey {username}:{api_key}"},
        )
    # ... same validation logic, raise CommCareVerificationError on failure
```

Then remove `sync_to_async` wrappers at call sites (2 sites in `users/views.py`).

**Effort:** Low — pure HTTP function, no ORM.

### 2b. Tenant resolution (`apps/users/services/tenant_resolution.py`)

**Current:** Three sync functions using `requests` + Django ORM:
- `_fetch_all_domains(access_token)` — paginated `requests.get()` to CommCare API
- `resolve_commcare_domains(user, access_token)` — calls `_fetch_all_domains` + ORM upserts
- `resolve_connect_opportunities(user, access_token)` — `requests.get()` to Connect API + ORM upserts

Callers in `apps/users/views.py:52,65,316` wrap with `sync_to_async`.

**Convert to:**

```python
async def _afetch_all_domains(access_token: str) -> list[dict]:
    async with httpx.AsyncClient(timeout=30) as client:
        results = []
        url = COMMCARE_DOMAIN_API
        while url:
            resp = await client.get(url, headers={"Authorization": f"Bearer {access_token}"})
            # ... same pagination logic
        return results

async def aresolve_commcare_domains(user, access_token: str) -> list[TenantMembership]:
    domains = await _afetch_all_domains(access_token)
    memberships = []
    for domain in domains:
        tenant, _ = await Tenant.objects.aupdate_or_create(
            provider="commcare", external_id=domain["domain_name"],
            defaults={"canonical_name": domain["project_name"]},
        )
        tm, _ = await TenantMembership.objects.aget_or_create(user=user, tenant=tenant)
        await TenantCredential.objects.aget_or_create(
            tenant_membership=tm,
            defaults={"credential_type": TenantCredential.OAUTH},
        )
        memberships.append(tm)
    return memberships

async def aresolve_connect_opportunities(user, access_token: str) -> list[TenantMembership]:
    # Same pattern: httpx.AsyncClient + async ORM
```

Then remove `sync_to_async` wrappers at call sites (3 sites in `users/views.py`).

Keep the sync versions for any non-async callers (e.g. management commands).

**Effort:** Medium — HTTP + ORM combined, paginated loop.

### 2c. Token refresh (`apps/users/services/token_refresh.py`)

**Current:** `refresh_oauth_token` uses `requests.post()` + `social_token.save()`.
Called via `_resolve_oauth_credential` in `credential_resolver.py:106`.

**Convert to:**

```python
async def arefresh_oauth_token(social_token, token_url: str) -> str:
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(token_url, data={...})
        response.raise_for_status()
    data = response.json()
    social_token.token = data["access_token"]
    # ... update fields
    await social_token.asave()
    return social_token.token
```

Then convert `_resolve_oauth_credential` in `credential_resolver.py` to async,
removing its `sync_to_async` wrapper.

**Effort:** Medium — HTTP + ORM save + called from `aresolve_credential`.

### 2d. Credential resolver (`apps/users/services/credential_resolver.py`)

**Current:** `aresolve_credential` is already async but wraps `_resolve_oauth_credential`
with `sync_to_async` because `refresh_oauth_token` uses sync `requests`.

**After 2c:** Once `arefresh_oauth_token` exists, make `_resolve_oauth_credential`
async too (rename to `_aresolve_oauth_credential`), removing the last `sync_to_async`
in this file.

**Effort:** Low — depends on 2c completing first.

### 2e. `_create` with `transaction.atomic` (`apps/users/views.py:188-207`)

**Current:** Inner function `_create()` uses `transaction.atomic()` for a
get_or_create + update_or_create sequence, wrapped with `sync_to_async`.

**Options:**
1. **Keep as-is** — `transaction.atomic` doesn't have an async API in Django 5.2.
   The `sync_to_async` wrapper is correct here.
2. **Replace with individual async ORM calls** — `aget_or_create` + `aupdate_or_create`
   without an explicit atomic block. Risk: non-atomic multi-step operation. The
   get_or_create calls are idempotent so this is safe in practice.
3. **Wait for Django async transactions** — expected in a future Django release.

**Recommendation:** Option 2 — the operations are all idempotent upserts. No
data integrity risk from removing the atomic wrapper.

**Effort:** Low.

### Phase 2 summary

| Call site | File | Blocked by |
|---|---|---|
| `sync_to_async(verify_commcare_credential)` ×2 | `apps/users/views.py:172,256` | 2a |
| `sync_to_async(resolve_commcare_domains)` | `apps/users/views.py:52` | 2b |
| `sync_to_async(resolve_connect_opportunities)` ×2 | `apps/users/views.py:65,316` | 2b |
| `sync_to_async(_resolve_oauth_credential)` | `credential_resolver.py:106` | 2c |
| `sync_to_async(_create)` | `apps/users/views.py:207` | 2e |

**Total:** 7 `sync_to_async` call sites removed. After Phase 2, `apps/users/`
is fully async with zero `sync_to_async`.

---

## Phase 3: Psycopg async — managed database queries

**Goal:** Migrate the MCP query service from `psycopg.connect()` (sync) to
`psycopg.AsyncConnection` with an async connection pool. This unlocks all
metadata service functions and the remaining `sync_to_async` calls in
`mcp_server/` and `apps/agents/graph/base.py`.

**Dependency:** None (psycopg 3 already supports async natively).

### 3a. Query service (`mcp_server/services/query.py`)

**Current:** `_execute_sync` and `_execute_sync_parameterized` use
`psycopg.connect()` with sync cursors. Wrapped by `sync_to_async` at the two
call sites in the same file (`execute_query`, `execute_internal_query`).

**Convert to:**

```python
import psycopg

async def _get_async_connection(ctx: QueryContext):
    return await psycopg.AsyncConnection.connect(**ctx.connection_params, autocommit=True)

async def _execute_async(ctx: QueryContext, sql: str, timeout_seconds: int) -> dict[str, Any]:
    async with await _get_async_connection(ctx) as conn:
        async with conn.cursor() as cursor:
            await cursor.execute(psql.SQL("SET ROLE {}").format(psql.Identifier(ctx.readonly_role)))
            try:
                await cursor.execute(...)
                # ... same logic
            finally:
                await cursor.execute("RESET ROLE")
```

Consider using `psycopg_pool.AsyncConnectionPool` for connection reuse.

**Effort:** Medium — core query path, needs careful testing.

### 3b. Metadata service (`mcp_server/services/metadata.py`)

**Current:** `pipeline_describe_table`, `workspace_list_tables`, and
`pipeline_get_metadata` call `_execute_sync_parameterized` directly (sync).
`pipeline_list_tables` uses sync Django ORM.

**After 3a:** Once the query functions are async:
- `pipeline_describe_table` → calls async `_execute_async_parameterized`
- `workspace_list_tables` → calls async `_execute_async_parameterized`
- `pipeline_get_metadata` → calls the above two
- `pipeline_list_tables` → convert ORM calls to async (`afirst()`, `async for`)

Then convert `transformation_aware_list_tables` to async as well (it calls
`pipeline_list_tables` + sync ORM).

**Effort:** Medium — multiple functions, but each conversion is mechanical.

### 3c. MCP server tool handlers (`mcp_server/server.py`)

**Current:** Tool handlers wrap metadata functions with `sync_to_async`:

| Line | Call |
|---|---|
| 101 | `sync_to_async(workspace_list_tables)(ctx)` |
| 129 | `sync_to_async(pipeline_list_tables)(ts, pipeline_config)` |
| 182 | `sync_to_async(pipeline_describe_table)(...)` |
| 240 | `sync_to_async(pipeline_get_metadata)(...)` |
| 732 | `sync_to_async(workspace_list_tables)(ctx)` |

**After 3b:** Call the async metadata functions directly. Remove all
`sync_to_async` wrappers.

**Effort:** Low — just removing wrappers once metadata functions are async.

### 3d. Agent graph schema context (`apps/agents/graph/base.py`)

**Current:** `_fetch_schema_context` wraps metadata functions:

| Line | Call |
|---|---|
| 183 | `sync_to_async(transformation_aware_list_tables)(...)` |
| 187 | `sync_to_async(pipeline_list_tables)(...)` |
| 205 | `sync_to_async(pipeline_describe_table)(...)` |

**After 3b:** Call async versions directly. Remove `from asgiref.sync import sync_to_async`.

**Effort:** Low.

### 3e. SchemaManager DDL (`mcp_server/server.py:800,809`)

**Current:** `teardown_schema` tool wraps `SchemaManager.teardown()` and
`teardown_view_schema()` with `sync_to_async`. These use `psycopg.connect()`
for DDL (CREATE/DROP SCHEMA).

**Convert:** Make SchemaManager methods async using `psycopg.AsyncConnection`.

**Priority:** Lowest — teardown runs infrequently, thread pool overhead is
negligible for DDL operations.

**Effort:** Medium — SchemaManager has multiple methods with psycopg usage.

### 3f. Materializer (`mcp_server/server.py:506`)

**Current:** `run_pipeline` is a massive sync orchestrator (HTTP + psycopg +
dbt + progress callbacks). Wrapped with `sync_to_async` — runs in thread pool.

**Recommendation:** Keep as-is. This is a long-running operation (minutes) that
benefits from running in a dedicated thread. Converting it would require
rewriting the entire materializer, all loaders, and the dbt runner. The
thread pool is the correct pattern here.

**Effort:** N/A — not recommended.

### Phase 3 summary

| Call site | File | Blocked by |
|---|---|---|
| `sync_to_async(_execute_sync)` | `query.py:142` | 3a |
| `sync_to_async(_execute_sync_parameterized)` | `query.py:110` | 3a |
| `sync_to_async(workspace_list_tables)` ×2 | `server.py:101,732` | 3b |
| `sync_to_async(pipeline_list_tables)` | `server.py:129` | 3b |
| `sync_to_async(pipeline_describe_table)` | `server.py:182` | 3b |
| `sync_to_async(pipeline_get_metadata)` | `server.py:240` | 3b |
| `sync_to_async(transformation_aware_list_tables)` | `graph/base.py:183` | 3b |
| `sync_to_async(pipeline_list_tables)` | `graph/base.py:187` | 3b |
| `sync_to_async(pipeline_describe_table)` | `graph/base.py:205` | 3b |
| `sync_to_async(mgr.teardown_view_schema)` | `server.py:800` | 3e |
| `sync_to_async(mgr.teardown)` | `server.py:809` | 3e |

**Total:** 11 `sync_to_async` call sites removed. After Phase 3, `mcp_server/`
and `apps/agents/graph/base.py` are fully async.

---

## Intentionally kept

| Call site | File | Reason |
|---|---|---|
| `async_to_sync(self._build_graph)()` | `apps/recipes/services/runner.py:196` | Sync entry point (Celery/management command) calling async code. Correct pattern. |
| `sync_to_async(run_pipeline)` | `mcp_server/server.py:506` | Long-running sync orchestrator. Thread pool is the right approach. |

---

## Phase ordering

```
Phase 2 (httpx)          Phase 3 (psycopg async)
    │                         │
    ├─ 2a: verification       ├─ 3a: query service
    ├─ 2b: resolution         ├─ 3b: metadata service (depends on 3a)
    ├─ 2c: token refresh      ├─ 3c: MCP server tools (depends on 3b)
    ├─ 2d: credential resolver├─ 3d: agent graph (depends on 3b)
    └─ 2e: atomic block       ├─ 3e: SchemaManager DDL (low priority)
                              └─ 3f: materializer (keep as-is)
```

Phases 2 and 3 are independent and can be worked in parallel.

After both phases: only 2 `sync_to_async`/`async_to_sync` calls remain in
production code — `run_pipeline` (correct) and `RecipeRunner.execute` (correct).
