# Procrastinate-Backed Materialization with Page-Level Progress and Cancellation

**Date:** 2026-04-30
**Status:** Draft — pending architectural review
**Author:** Brian DeRenzi

> **Note:** This plan assumes the Celery → Procrastinate migration in PR #166
> (`bdr/procrastinate`) has landed. See `docs/plans/2026-05-01-celery-to-procrastinate-design.md`
> for the migration design. This plan adopts the same conventions: tasks are
> `async def`, decorated with `@app.task` from `config.procrastinate`, and
> dispatched via `await task.defer_async(...)`.

---

## Background

When a user asks the Scout agent to load data (e.g. "how many chats have happened"), the agent calls the `run_materialization` MCP tool, which synchronously runs `run_pipeline()` inside the MCP server process. That function paginates through external APIs (OCS, CommCare Connect, CommCare HQ), writing each page to the managed PostgreSQL database.

The problem: during each paginated API request, no data flows over the SSE connection back to the browser. nginx has a default `proxy_read_timeout` of 60 seconds. If a single API page takes longer than 60 seconds to return — which happens with large datasets — nginx drops the connection and the user sees a "network error."

This has already affected production (scout.dimagi.com, 2026-04-30) during OCS session loading. A partial workaround exists in the labs/embedded nginx config (`proxy_read_timeout 600s`) but was never applied to the main deployment config.

Increasing timeouts is a bandaid. The correct fix is to decouple the data loading work from the HTTP request lifecycle.

---

## Goals

1. Eliminate SSE connection drops caused by slow external API calls during materialization.
2. Give users real-time, meaningful progress: percentage complete and row counts updated after each page of data.
3. Allow users to cancel an in-progress materialization from the chat UI.
4. Lay the groundwork for proper cancellation support (the `procrastinate_job_id` field enables `app.job_manager.cancel_job_by_id_async(..., abort=True)` in a follow-on).

## Non-Goals

- Resume or retry after cancellation.
- A dedicated materialization status page or admin view.
- Parallel materialization across tenants (sequential remains the default).
- Changes to `refresh_tenant_schema` (the existing automated schema refresh task — separate concern).

---

## Architecture

### Current flow

```
Browser ←SSE─ Django ← LangGraph ← MCP tool ─blocks─► run_pipeline() ─► OCS API
                                                                           (silent 60s+)
```

### Proposed flow

```
Browser ←SSE─ Django ← LangGraph ← MCP tool ─polls DB every 3s─► MaterializationRun.progress
                                                                          ↑ writes progress
                              Procrastinate worker ─► run_pipeline() ─► OCS API

Cancel path:
  Browser ──► POST /api/workspaces/{id}/materialization/cancel/
                ─► mark run CANCELLED in DB
                ─► app.job_manager.cancel_job_by_id_async(job_id, abort=True)
                ─► run_pipeline's progress_updater sees CANCELLED, raises -> rollback
                ─► MCP polling loop sees CANCELLED, returns to agent
```

**No agent changes required.** The agent still calls `run_materialization` as a single tool invocation and receives a single result. The polling is internal to the MCP tool.

**No new SSE infrastructure required.** The MCP tool emits MCP progress notifications on each DB poll, which flow through the existing `progress_queue` → SSE pipeline in `apps/chat/stream.py`.

---

## Data Model Changes

### `MaterializationRun` — new fields and state

**New state:**

```python
class RunState(models.TextChoices):
    STARTED      = "started"
    DISCOVERING  = "discovering"
    LOADING      = "loading"
    TRANSFORMING = "transforming"
    COMPLETED    = "completed"
    FAILED       = "failed"
    CANCELLED    = "cancelled"   # new
```

**New fields:**

```python
procrastinate_job_id = models.BigIntegerField(null=True, blank=True, db_index=True)
progress = models.JSONField(null=True, blank=True)
```

Procrastinate job IDs are bigint primary keys on `procrastinate_jobs`. We don't add a real FK because procrastinate purges completed jobs and we want the run record to outlive the job row.

**`progress` field shape:**

```json
{
  "step": 4,
  "total_steps": 7,
  "source": "sessions",
  "message": "Loading sessions from OCS API...",
  "rows_loaded": 500,
  "rows_total": 13028
}
```

`rows_total` is `null` when the API's pagination envelope does not include a total count (see [Progress Formatting](#progress-formatting) below).

A Django migration is required for both fields.

---

## Component Changes

### 1. `run_pipeline()` — progress updater

Add an optional `progress_updater: Callable[[dict], None]` parameter. The existing `report()` helper calls it at each phase transition (provision, discover, load source, transform). This replaces the current `progress_callback` parameter (which was a simpler `(current, total, message)` tuple).

```python
def run_pipeline(
    tenant_membership,
    credential,
    pipeline,
    progress_updater=None,   # new; replaces progress_callback
) -> dict:
```

`progress_updater` receives the full progress dict (step, total_steps, source, message, rows_loaded, rows_total), which the worker writes to `MaterializationRun.progress` using `update_fields=["progress"]`.

**Cooperative cancellation:** `progress_updater` is also the cancellation checkpoint. After writing progress, it re-reads `MaterializationRun.state`; if it sees `CANCELLED`, it raises `MaterializationCancelled` (a new exception). Because `run_pipeline` runs sync inside `asyncio.to_thread()`, the check uses sync ORM. The exception propagates up through `_load_source` and is caught in the task wrapper, which leaves the open psycopg transaction to roll back automatically when the connection closes.

### 2. Writer functions — per-page callbacks

Each writer function (`_write_ocs_sessions`, `_write_ocs_messages`, `_write_connect_visits`, etc.) gains an optional `on_page(rows_loaded: int, rows_total: int | None) -> None` parameter. After each `executemany` call, it invokes `on_page(total_so_far, rows_total)`. The caller (`_load_source`) passes this callback through from `progress_updater`.

### 3. Base loaders — capture total count

`OCSBaseLoader._paginate()` and `ConnectBaseLoader._paginate_export_pages()` currently discard the `count` field from the pagination envelope. Update both to capture it from the **first page response only** and yield `(page, total_count | None)` on the first iteration, then `(page, None)` on subsequent pages. (The total only needs to be read once.)

- OCS uses cursor-based pagination — `count` may not be present. Degrade gracefully.
- CommCare Connect uses a similar cursor pattern — same treatment.
- CommCare HQ uses offset pagination — `count` is present.

### 4. New Procrastinate task: `materialize_workspace`

New task in `apps/workspaces/tasks.py`, alongside the existing `refresh_tenant_schema` and friends:

```python
@app.task(pass_context=True)
async def materialize_workspace(
    context,
    workspace_id: str,
    user_id: str,
) -> dict:
    """Run materialization for all tenants in a workspace.

    Writes progress to MaterializationRun.progress after each page so the
    MCP polling loop can surface real-time status to the user.
    """
```

Steps:
1. Resolve workspace and `TenantMembership` records (same logic as the current MCP tool's `run_materialization`).
2. For each tenant membership, resolve credential and identify pipeline.
3. Create a `MaterializationRun` record with `procrastinate_job_id = context.job.id`.
4. Build a sync `progress_updater` closure that:
   - Writes the dict to `MaterializationRun.progress` (sync ORM, since we're inside `to_thread`).
   - Re-reads `MaterializationRun.state`; if `CANCELLED`, raises `MaterializationCancelled`.
5. `await asyncio.to_thread(run_pipeline, membership, credential, pipeline_config, progress_updater=progress_updater)`.
6. On success: `run.state = COMPLETED`. On `MaterializationCancelled`: `run.state` already CANCELLED, exit cleanly. On other failure: `run.state = FAILED`.

Dispatch from the MCP tool:

```python
job_id = await materialize_workspace.defer_async(
    workspace_id=str(workspace_id),
    user_id=str(user_id),
)
```

Procrastinate calls task functions by keyword arguments only; the dispatch above mirrors the kwargs-style already used for `teardown_schema.defer_async(schema_id=...)`. The returned `job_id` is informational here — the MCP tool finds the run by polling `MaterializationRun` records for the workspace, not by job ID.

The existing `refresh_tenant_schema` task is unchanged — it handles automated schema refreshes on a different lifecycle path.

### 5. `run_materialization` MCP tool — refactored

Current behaviour: calls `sync_to_async(run_pipeline)(...)`, blocks until done.

New behaviour:

```python
async def run_materialization(workspace_id, user_id, ctx):
    # 1. Defer the Procrastinate task
    await materialize_workspace.defer_async(
        workspace_id=str(workspace_id),
        user_id=str(user_id),
    )

    # 2. Poll the DB until done, cancelled, or timed out
    deadline = time.monotonic() + 600  # 10-minute absolute cap
    while time.monotonic() < deadline:
        await asyncio.sleep(3)
        progress = await _query_workspace_progress(workspace_id)

        # Emit MCP progress notification — flows through existing progress_queue → SSE
        if ctx and progress.message:
            await ctx.report_progress(progress.step, progress.total_steps, progress.message)

        if progress.all_done or progress.cancelled:
            break

    return progress.to_result_dict()
```

`_query_workspace_progress(workspace_id)` queries the latest `MaterializationRun` records for the workspace and returns an aggregated status object. It tolerates "no run yet" (the worker may not have created the row at the time of the first poll) by returning a "queued" status.

> **Note (out of scope, but noticed):** the MCP tool's existing path already
> uses `sync_to_async(run_pipeline)` with the default `thread_sensitive=True`,
> which serializes long-running dbt runs behind ORM calls. The same observation
> appears in PR #166's design doc. Worth tracking; not addressed here.

### 6. Cancel endpoint

**`POST /api/workspaces/{workspace_id}/materialization/cancel/`**

Async Django view, `@async_login_required`:

1. Verify the requesting user has workspace membership.
2. Find all `MaterializationRun` records for the workspace in an active state (STARTED, DISCOVERING, LOADING, TRANSFORMING).
3. Bulk-update those runs: `state=CANCELLED, completed_at=now()` **first** so the in-process `progress_updater` sees CANCELLED on its next checkpoint.
4. For each run with a non-null `procrastinate_job_id`, call `await app.job_manager.cancel_job_by_id_async(run.procrastinate_job_id, abort=True)`. This:
   - Cancels the job outright if it is still queued (status = `todo`).
   - Marks a running job for abort. Procrastinate's async-task abort path raises `asyncio.CancelledError` at the next `await` boundary — but our work happens inside `asyncio.to_thread`, where CancelledError will not interrupt the running thread. The cooperative DB-state check in `progress_updater` is what actually stops the loader between pages.
5. Return `{"status": "cancelled", "runs_cancelled": N}`.

**Partial data safety:** `run_pipeline` wraps all source writes in a single psycopg transaction (`autocommit=False`, explicit `conn.commit()` at end of LOAD phase). When `progress_updater` raises `MaterializationCancelled`, the exception unwinds out of the writer code without reaching the `conn.commit()`. The connection is then closed in the `finally` block, and PostgreSQL rolls back the uncommitted transaction. No partial data is written to the schema.

**State after cancel:** The schema remains in whatever state it was in before the run started. A subsequent `run_materialization` call starts a fresh run.

**Cancellation latency:** Up to one API page. Cooperative abort means we only stop between pages, so a single page that takes 90s will block the cancel for up to 90s. This is the trade-off vs. Celery's `revoke(terminate=True)`, which signals the worker process directly. With shared procrastinate workers we don't want to kill the process — other unrelated jobs may be running on it.

---

## Progress Formatting

The MCP progress notification message (shown in the tool card as `⏳ <message>`) is built inside `_query_workspace_progress` from the current `MaterializationRun.progress` field:

**With total row count available:**
```
Loading sessions from OCS API... 4.6% (500 / 13,028 rows)
Loading sessions from OCS API... 15.3% (2,000 / 13,028 rows)
Loading sessions from OCS API... 100% (13,028 / 13,028 rows)
```

**Without total row count (cursor pagination, no count field):**
```
Loading sessions from OCS API... 500 rows loaded
Loading sessions from OCS API... 2,000 rows loaded
```

**Non-load phases:**
```
Provisioning schema for my-experiment...
Discovering tenant metadata from ocs...
Running transforms...
```

**Percentage format:** one decimal place (e.g. `4.6%`, `15.3%`). At typical page sizes of ~1,000 rows, one decimal provides meaningful granularity without visual noise.

**Multi-tenant workspaces:** When a workspace has multiple tenants, the overall progress message prefixes the tenant name: `[tenant-id] Loading sessions... 4.6% (500 / 13,028 rows)`.

---

## Frontend: Cancel Button

The `run_materialization` tool card in `ToolCallPart` (`ChatMessage.tsx`) already auto-expands and shows progress notifications. A small stop button is added to the right of the card header, visible only while the tool is in-flight.

**Appearance:**
```
> 🔧 run_materialization...                    [■ Stop]
  ⏳ Loading sessions from OCS API... 4.6% (500 / 13,028 rows)
```

**Conditions for visibility:**
- `toolName === "run_materialization"`
- `isLoading` (state is `input-streaming` or `input-available`)
- `isActiveMessage` (not a historical message)

**Click behaviour:**
1. Calls `POST /api/workspaces/{workspaceId}/materialization/cancel/` using `workspaceId` from the app store.
2. Button transitions to "Cancelling..." and is disabled while the request is in-flight.
3. On success: button disappears. The agent receives `{"status": "cancelled"}` from the MCP tool on its next poll and informs the user naturally in the conversation.
4. On network error: button resets to "Stop" with a brief error indicator.

**Icon:** `Square` from lucide-react (the standard "stop" symbol), styled with `text-red-500/70 hover:text-red-500`. Small and unobtrusive — visible but not alarming.

A `data-testid="materialization-cancel-btn"` attribute is added for QA automation.

---

## nginx Defense-in-Depth

Add `proxy_read_timeout 120s; proxy_send_timeout 120s;` to the `/api/` location in `nginx.prod-kamal.conf`. With 3-second polling, 40 consecutive zero-response polls would be needed to trip this — it is a backstop, not the primary fix.

---

## Error Handling

| Scenario | Behaviour |
|---|---|
| Procrastinate worker crashes mid-load | psycopg connection drops; PostgreSQL rolls back uncommitted transaction automatically. The Procrastinate job row is left in `doing` state until the worker restarts and the job's heartbeat lapse triggers re-queueing (or it stays orphaned until manual cleanup). The `MaterializationRun` row stays in LOADING state until a stale-run cleanup (see Open Questions) marks it FAILED. |
| OCS API returns 401/403 | `run_pipeline` raises `OCSAuthError`. Task marks run FAILED. Polling loop returns `{"status": "failed", "error": "Auth token expired or missing"}` to agent. |
| OCS API slow but eventually responds | Normal operation — 3-second poll keeps connection alive, progress updates continue. |
| Cancel called when no run is active | Cancel endpoint returns `{"status": "no_active_run"}` with HTTP 200. |
| Polling loop hits 10-minute deadline | Returns `{"status": "timeout"}` to agent. Agent informs user. The Procrastinate job continues running; user can check status later. |
| Cancel endpoint called after run completes | `cancel_job_by_id_async` is a no-op on a finished job and returns 0 cancellations. Endpoint returns `{"status": "no_active_run"}`. |
| Cancel called between pages, mid-page-fetch | DB state set to CANCELLED. The in-flight HTTP page-fetch finishes (up to one page of latency). On the next `progress_updater` call, the loader raises `MaterializationCancelled`, the transaction rolls back, and the run record is already CANCELLED. |

---

## Testing

- **Unit:** `run_pipeline` with a mock `progress_updater`; verify progress dict structure and `on_page` call count.
- **Unit:** `run_pipeline` cancellation — `progress_updater` raises `MaterializationCancelled` after N pages; verify the partial data is not committed.
- **Unit:** `_paginate` count extraction — with count field, without, cursor vs offset.
- **Integration:** `materialize_workspace` task as `async def` — call directly with `@pytest.mark.asyncio` + `@pytest.mark.django_db(transaction=True)` (the same pattern already in use for the converted procrastinate tasks in PR #166's `tests/test_refresh_task.py`); verify `MaterializationRun.progress` is written correctly.
- **Integration:** Cancel endpoint — patch `apps.workspaces.tasks.materialize_workspace.defer_async` (and the `app.job_manager.cancel_job_by_id_async` call) with `AsyncMock`; verify the job-cancel call happens with `abort=True` and the run state transitions to CANCELLED.
- **Integration:** Polling loop — mock DB returning CANCELLED state; verify tool returns cancelled result.
- **E2E (QA):** Cancel button appears during active materialization; clicking it stops the run and the agent acknowledges cancellation.

---

## Open Questions

1. **OCS `count` field:** Does the OCS sessions/messages API actually return a `count` field in its pagination envelope? If cursor-based pagination omits it, we fall back to "N rows loaded" format. Needs a quick API check before implementation.

2. **Stale LOADING runs:** If a worker crashes without the cancel endpoint being called, the run stays in LOADING state indefinitely. Should a periodic procrastinate task (sibling to the existing `expire_inactive_schemas` cron at `*/30 * * * *`) handle this? Proposed: add a 30-minute stale-run cleanup that marks LOADING/DISCOVERING runs older than 60 minutes as FAILED. With Procrastinate's `procrastinate_jobs` table also tracking job status, the cleanup task can cross-reference to decide "is the worker still working on this?" before marking it FAILED.

3. **Multi-tenant cancellation granularity:** The cancel endpoint cancels all active runs for a workspace in one call. Should it support cancelling a single tenant's run? Probably not needed yet.

4. **Cancellation latency:** Up to one API page (potentially 60s+). If this proves unacceptable in practice, options include: (a) a per-run dedicated procrastinate queue + worker so we can safely SIGTERM the worker process; (b) adding finer-grained checkpoints inside loaders (e.g. before each HTTP request, not just after each page); (c) an HTTP-level abort on the loader's requests session, fed by a `threading.Event` set when CANCELLED is detected.
