# Materialization as a background job

**Status:** design approved
**Branch:** `bdr/materialization-background-jobs`
**Author:** Brian DeRenzi (with Claude)

## Problem

Materialization runs on a Procrastinate worker (good — no worker-side timeout), but the chat path re-blocks on completion:

- `mcp_server/server.py:654` (`run_materialization`) polls progress with a 600s deadline.
- `apps/chat/stream.py:39` wraps the whole agent step in a 300s hard ceiling (`AGENT_TIMEOUT_SECONDS`).

The 300s ceiling fires first. Large tenants (e.g. 81k+ cases) trip it, the chat shows "The request timed out…", and the user has no visibility into the still-running worker, no way to cancel it from outside the dead chat turn, and no way to learn when it finished.

Today there is no `GET` status endpoint, no per-thread or workspace-level "background job" UI affordance, and no mechanism to resume the conversation when materialization completes.

## Goal

Decouple `run_materialization` from the chat turn so:

1. The chat agent acknowledges and ends its turn in well under a second.
2. Progress is durably visible (sidebar spinner + percent, in-chat live progress) across page navigation and refreshes.
3. When materialization completes, the agent automatically resumes the conversation in the original thread with the loaded data ready to query.
4. Cancellation works from the chat panel today; the design leaves room for cancel from other surfaces.
5. The same machinery generalizes to other long-running async tasks later (no extra UI work to plug them in).

## Architecture

```
chat POST ─► agent ─► run_materialization (MCP)
                                │
                                ├─► creates ThreadJob row (new model)
                                ├─► defers materialize_workspace (Procrastinate)
                                └─► returns {"status":"started", "thread_job_id":...} immediately

agent emits "I've kicked off the materialization, I'll continue once it's done"
agent turn ENDS  ← chat no longer holds a long connection

   ─────────────  (worker runs for minutes)  ─────────────

Procrastinate worker finishes materialize_workspace
                                │
                                ▼
              resume_thread_after_materialization (chained Procrastinate task)
                                │
                ┌───────────────┼───────────────┐
                ▼               ▼               ▼
        marks ThreadJob   loads LangGraph    re-invokes the
        COMPLETED         conversation       agent server-side
                          state              (no client attached)
                                │
                                ▼
                          new agent message
                          persisted via checkpointer

Frontend polls /api/workspaces/<id>/jobs/active/ every 3s while ≥1 job is open
   - sidebar shows spinner + percent on the thread that started it
   - main chat shows in-card live progress (current UX, rewired to new data source)
   - on COMPLETED → refetch thread messages, swap spinner for green dot
   - opening the thread clears the dot (last_viewed_at)
```

## Data model

### New: `apps/workspaces/models.py` — `ThreadJob`

```python
class ThreadJob(models.Model):
    class JobType(models.TextChoices):
        MATERIALIZATION = "materialization"

    class State(models.TextChoices):
        PENDING = "pending"       # row created, procrastinate job dispatched
        RUNNING = "running"       # worker picked it up
        COMPLETED = "completed"
        FAILED = "failed"
        CANCELLED = "cancelled"

    id = UUIDField(primary_key=True, default=uuid4)
    thread = FK("chat.Thread", on_delete=CASCADE, related_name="jobs")
    job_type = CharField(choices=JobType.choices, max_length=32)
    procrastinate_job_id = BigIntegerField(unique=True, db_index=True)
    tool_call_id = CharField(max_length=64)  # the agent's tool_call_id, used by resume
    state = CharField(choices=State.choices, default=State.PENDING, max_length=16)
    created_at = DateTimeField(auto_now_add=True)
    completed_at = DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            Index(fields=["thread", "state"]),
            Index(fields=["procrastinate_job_id"]),
        ]
```

Indexed by `thread_id, state` (sidebar lookups) and `procrastinate_job_id` (resume hook).

### Change: `apps/chat/models.py` — `Thread.last_viewed_at`

Add one nullable column:
```python
last_viewed_at = DateTimeField(null=True, blank=True)
```
NULL means "never viewed" — no false-positive green dots for newly-created threads. Updated whenever the user opens the thread in the UI.

(No separate `ThreadView` join table is needed because `Thread` is already per-(workspace, user) — see `apps/chat/models.py:8`.)

## HTTP endpoints

| Verb | Path | Purpose |
|------|------|---------|
| GET  | `/api/workspaces/<id>/jobs/active/` | Active `ThreadJob`s for the current user in this workspace, enriched with latest `MaterializationRun.progress`. Polled by `useWorkspaceJobs` every 3s while jobs are open. |
| POST | `/api/workspaces/<id>/jobs/<thread_job_id>/cancel/` | Generic cancel. For `job_type=materialization`, delegates to the existing materialization cancel logic. |
| POST | `/api/workspaces/<id>/threads/<thread_id>/viewed/` | Update `Thread.last_viewed_at` (called from the chat UI on thread open). |

The existing `POST /api/workspaces/<id>/materialization/cancel/` stays for backward compatibility; both endpoints converge on the same internal cancel function and additionally flip the corresponding `ThreadJob.state = CANCELLED`.

**Response shape for `/jobs/active/`:**
```json
{
  "jobs": [
    {
      "thread_job_id": "uuid",
      "thread_id": "uuid",
      "job_type": "materialization",
      "state": "running",
      "progress": {
        "percent": 64,               // null if rows_total is null
        "rows_loaded": 64000,
        "rows_total": 100000,        // can be null for indeterminate APIs
        "message": "Loading cases from commcare API...",
        "source": "cases",
        "step": 3,
        "total_steps": 5
      },
      "created_at": "2026-05-20T14:00:00Z"
    }
  ]
}
```

## MCP tool change

`mcp_server/server.py:run_materialization` becomes fire-and-acknowledge:

```python
async def run_materialization(workspace_id, user_id, ctx, _thread_id, _tool_call_id):
    # ...validate + resolve memberships (unchanged)
    job = await materialize_workspace.defer_async(
        workspace_id=str(workspace_id),
        user_id=str(user_id) if user_id else "",
    )
    thread_job = await ThreadJob.objects.acreate(
        thread_id=_thread_id,
        job_type=ThreadJob.JobType.MATERIALIZATION,
        procrastinate_job_id=job.id,
        tool_call_id=_tool_call_id,
        state=ThreadJob.State.PENDING,
    )
    return success_response({
        "status": "started",
        "thread_job_id": str(thread_job.id),
        "message": "Materialization started in background. I'll continue when it finishes.",
    })
```

The 600s poll loop, the `progress_queue` plumbing in `apps/chat/views.py:137`, and the `_PROGRESS_TOOLS`/`on_tool_start` progress handling in `apps/chat/stream.py` are deleted. Net code reduction.

Two new injected params (`_thread_id`, `_tool_call_id`) extend the existing pattern (`workspace_id`/`user_id` are already injected server-side — see `apps/agents/graph/base.py:336`). Implementation:

- `thread_id` is added to `AgentState` (`apps/agents/graph/state.py:80`) and to the `injections` dict in `build_agent_graph` — same pattern as `workspace_id`.
- `tool_call_id` is **per-call**, not state-based. Extend `_make_injecting_tool_node` (`apps/agents/graph/base.py:279`) so that for matching MCP tool calls it also injects `tc["id"]` as `_tool_call_id` into the tool's args. LangChain's `tool_calls` already carry the id (see line 384).

## Agent prompt change

System prompt for `run_materialization` updated to (in `apps/agents/prompts/`):

> Returns immediately with `status: started`. Acknowledge briefly to the user (1 sentence) and end your turn — do NOT call other data tools yet. The system will resume the conversation automatically when materialization completes, with the loaded data ready to query.

## Resume task

`apps/workspaces/tasks.py` — new task:

```python
@app.task
async def resume_thread_after_materialization(thread_job_id: str) -> dict:
    # 1. Load ThreadJob + Thread; bail if state is already terminal.
    # 2. Aggregate MaterializationRun results for this procrastinate_job_id
    #    into a summary dict {status, tenants, all_succeeded}.
    # 3. Append a HumanMessage with a system-framed notification:
    #      "[System notification: materialization just completed
    #       (status=completed|cancelled|failed). Please continue with the
    #       user's original request.]"
    #    This is simpler than re-using tool_call_id (which already has a
    #    "started" ToolMessage attached and must not be clobbered) and lets
    #    the agent re-read the conversation, see the original prompt, and
    #    respond — querying fresh data as needed.
    # 4. Invoke the agent for that thread (ainvoke, not astream). No client
    #    SSE stream; the checkpointer persists the new agent message(s).
    # 5. Mark ThreadJob COMPLETED / FAILED / CANCELLED based on aggregated
    #    state.
```

**Chaining:** at the end of `materialize_workspace` (after the per-tenant loop), defer `resume_thread_after_materialization.defer_async(thread_job_id=...)`. Runs whether materialization succeeded, failed, or was cancelled — the agent handles each case per Option A.

**Hidden-message UX note:** the synthetic "[System notification…]" HumanMessage is filtered out at the chat-history fetch boundary so it doesn't render in the UI; only the agent's resulting response is shown to the user. The conversation looks natural: user message → agent acknowledgment → agent analysis. The marker on filtering: a sentinel prefix (e.g. `"[__system_resume__]"`) or a custom message subclass. Pick at implementation time.

## Frontend

### Shared polling hook

`frontend/src/hooks/useWorkspaceJobs.ts` — lives at the workspace layout level (above sidebar + chat panel).

- Polls `/api/workspaces/<id>/jobs/active/` every 3s while ≥1 job is active.
- Pauses polling entirely when no jobs are active. Re-armed when a chat tool call fires (the chat panel can call a `notifyJobStarted()` callback exposed by the hook, or the hook can simply re-poll on any new SSE `tool-input-available` for `run_materialization`).
- On state transition `PENDING|RUNNING → COMPLETED|FAILED|CANCELLED`, the hook triggers a one-shot refetch of that thread's messages (so the resumed agent message appears in the chat without a manual refresh).

### Sidebar — per-thread indicators

```
┌─────────────────────────────────────┐
│ ▣  Sales analysis Q1     ⟳ 64%      │  active job
│ ▣  User funnel           ●          │  unread completion
│ ▣  Forms by month                   │  idle
└─────────────────────────────────────┘
```

- **Spinner + percent**: thread has an active `ThreadJob`. Tooltip shows the full progress message ("Loading cases from commcare API… 64,000/100,000 rows").
- **Indeterminate spinner (no number)**: progress reported but `rows_total` is null.
- **Green dot**: thread has a terminal-state job whose resume message is newer than `last_viewed_at`. Cleared when the user opens the thread (POST `/threads/<id>/viewed/`).
- Sidebar Stop button is **deferred** for v1 — the in-chat Stop button covers the common case.

### Main chat panel

- The `run_materialization` tool card (`frontend/src/components/ChatMessage/ChatMessage.tsx:159`) keeps its live-progress rendering, but the data source flips: instead of consuming `tool-output-available` SSE chunks pushed by the (deleted) poll loop, it reads from `useWorkspaceJobs` filtered to this thread.
- The card stays "open" while `state ∈ {PENDING, RUNNING}` and shows the final summary on transition.
- The existing **Stop** button (`ChatMessage.tsx:206`) is rewired to POST to `/jobs/<thread_job_id>/cancel/`.

### Files

- New: `frontend/src/hooks/useWorkspaceJobs.ts`
- New: `frontend/src/api/jobs.ts`
- Edit: chat sidebar thread-list component (rendering location TBD at implementation time)
- Edit: `frontend/src/components/ChatMessage/ChatMessage.tsx`

## Edge cases & failure modes

### Race conditions

- **User sends msg2 mid-materialization.** Agent processes msg2 with whatever data is currently available (likely none — will say so). When materialization completes, the resume turn runs *after* msg2's response. Resume sees the full conversation history and can offer to revisit the earlier question.
- **Refresh during materialization.** State is in DB; hook re-polls on mount; sidebar repopulates. No client recovery code.
- **Two materializations in different threads, same workspace.** Procrastinate dispatches both. Each is independent per-tenant_schema. Sidebar shows two spinners. **Sanity check during implementation**: confirm no race risk on shared tenant_schema; add workspace-level lock at dispatch if needed.
- **Resume races with cancel.** Cancel flips `ThreadJob.state = CANCELLED` and aborts the procrastinate job. The chained resume task starts by re-reading `ThreadJob.state`; if `CANCELLED`, it builds a "cancelled" tool result and lets the agent respond (Option A path). Idempotent.

### Failure modes

- **Worker dies mid-materialization.** `ThreadJob` sits in PENDING/RUNNING. New janitor task `expire_stale_thread_jobs` (hooked into `expire_inactive_schemas` cron at `apps/workspaces/tasks.py:242`) flips orphaned ThreadJobs to FAILED and fires the resume so the user isn't stuck with a phantom spinner.
- **Resume task itself fails.** Mark `ThreadJob.state = FAILED`, append a fixed "[Materialization completed but I couldn't continue automatically — try asking again]" assistant message to the thread. Materialized data is still there; user re-prompts.
- **ThreadJob.acreate() fails after dispatch.** Roll back the Procrastinate dispatch (`job_manager.cancel_job_by_id_async`) and return an error to the agent. Rare; not building distributed-transaction infra.

## Cancel

- Existing in-chat Stop button: rewired to `/jobs/<thread_job_id>/cancel/`.
- Backend: existing `materialization_cancel_view` continues to exist for back-compat; both cancel endpoints share the internal cancel function which now also flips `ThreadJob.state = CANCELLED`. The chained `resume_thread_after_materialization` reads the cancelled state and produces a graceful agent response (Option A).
- Sidebar/workspace-level cancel UI: **deferred**.

## Testing

### Backend
- Unit: `ThreadJob` state transitions; resume task with mocked agent (asserts the ToolMessage is injected with the right `tool_call_id` and persisted).
- Integration: `materialize_workspace` → `resume_thread_after_materialization` chain with the existing fake CommCare API fixtures; assert agent message appears in checkpointer state.
- Cancel: cancel during PENDING, during RUNNING mid-page, and after worker finishes but before resume runs. All idempotent.
- Janitor: orphaned ThreadJob (no live procrastinate job) flipped to FAILED on cron tick.

### Frontend
- Component test: sidebar with mocked `useWorkspaceJobs` returning each state shape.
- playwright-cli scenario: trigger materialization → spinner appears → mock worker completion → green dot → click thread → dot clears.

## Out of scope (explicit)

- Workspace-level "Materialization in progress" top banner.
- Sidebar Stop button.
- Toast on completion.
- xmlns filtering on the forms loader (separate ask).
- Generalizing existing async tasks beyond materialization (the `ThreadJob.job_type` field makes this trivial later; no work in this change).

## Migration / rollout

- Two additive migrations: `ThreadJob` table, `Thread.last_viewed_at` column. No backfill needed.
- Deletes: 600s poll loop in `mcp_server/server.py`, `progress_queue` plumbing in `apps/chat/views.py` and `apps/chat/stream.py`. Strictly-better replacement; in-flight materializations at deploy time are caught by the janitor on the next worker tick.
