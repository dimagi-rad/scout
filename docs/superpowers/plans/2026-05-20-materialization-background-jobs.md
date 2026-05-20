# Materialization as Background Jobs — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Decouple the `run_materialization` MCP tool from the chat turn so large materializations stop hitting the 5-minute agent ceiling. Adds durable job tracking, a polled status endpoint, sidebar indicators, and a chained resume task that auto-continues the conversation when materialization completes.

**Architecture:** Materialization keeps running on the Procrastinate worker (unchanged). The MCP tool becomes fire-and-acknowledge — it dispatches a Procrastinate job, creates a `ThreadJob` record tying that job to a chat thread, and returns `status: started` immediately. The agent ends its turn with a brief acknowledgment. A frontend hook polls `/api/workspaces/<id>/jobs/active/` for live progress, showing a spinner+percent on the sidebar thread row. When materialization finishes, a chained `resume_thread_after_materialization` task injects a system-framed HumanMessage into the conversation, re-invokes the agent server-side, and the new message persists via the LangGraph checkpointer. A `Thread.last_viewed_at` column drives a "green dot" unread indicator that clears when the user opens the thread.

**Tech Stack:** Django 5 (async), DRF, Procrastinate, LangGraph + langchain-anthropic, PostgreSQL, React 19 + Vite + TypeScript, Zustand store.

**Reference spec:** `docs/superpowers/specs/2026-05-20-materialization-background-jobs-design.md`

---

## Phase 1 — Backend data model

### Task 1: `ThreadJob` model

**Files:**
- Modify: `apps/chat/models.py`
- Create: `apps/chat/migrations/0XXX_threadjob.py` (generated)
- Test: `tests/test_threadjob_model.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_threadjob_model.py
import pytest
from apps.chat.models import Thread, ThreadJob
from apps.workspaces.models import Workspace
from django.contrib.auth import get_user_model

User = get_user_model()


@pytest.mark.django_db
def test_threadjob_defaults_to_pending():
    user = User.objects.create_user(email="a@b.c", password="x")
    ws = Workspace.objects.create(name="W", created_by=user)
    thread = Thread.objects.create(workspace=ws, user=user)
    job = ThreadJob.objects.create(
        thread=thread,
        job_type=ThreadJob.JobType.MATERIALIZATION,
        procrastinate_job_id=42,
        tool_call_id="abc",
    )
    assert job.state == ThreadJob.State.PENDING
    assert job.completed_at is None


@pytest.mark.django_db
def test_threadjob_procrastinate_job_id_is_unique():
    user = User.objects.create_user(email="a@b.c", password="x")
    ws = Workspace.objects.create(name="W", created_by=user)
    thread = Thread.objects.create(workspace=ws, user=user)
    ThreadJob.objects.create(
        thread=thread, job_type="materialization",
        procrastinate_job_id=99, tool_call_id="x",
    )
    with pytest.raises(Exception):
        ThreadJob.objects.create(
            thread=thread, job_type="materialization",
            procrastinate_job_id=99, tool_call_id="y",
        )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_threadjob_model.py -v`
Expected: FAIL — `ThreadJob` not importable.

- [ ] **Step 3: Add the model to `apps/chat/models.py`**

Append to `apps/chat/models.py`:

```python
class ThreadJob(models.Model):
    """Tracks a long-running background job (materialization, etc.) tied to a chat thread.

    The frontend polls active jobs to drive sidebar indicators and live progress;
    the resume worker uses ``tool_call_id`` to inject completion into the
    LangGraph conversation when the job finishes.
    """

    class JobType(models.TextChoices):
        MATERIALIZATION = "materialization", "Materialization"

    class State(models.TextChoices):
        PENDING = "pending", "Pending"
        RUNNING = "running", "Running"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"

    TERMINAL_STATES = frozenset({State.COMPLETED, State.FAILED, State.CANCELLED})
    ACTIVE_STATES = frozenset({State.PENDING, State.RUNNING})

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    thread = models.ForeignKey(
        "chat.Thread", on_delete=models.CASCADE, related_name="jobs"
    )
    job_type = models.CharField(max_length=32, choices=JobType.choices)
    procrastinate_job_id = models.BigIntegerField(unique=True, db_index=True)
    tool_call_id = models.CharField(max_length=64)
    state = models.CharField(
        max_length=16, choices=State.choices, default=State.PENDING
    )
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["thread", "state"], name="chat_threadjob_th_state"),
        ]
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.job_type}({self.state}) for thread {self.thread_id}"
```

- [ ] **Step 4: Generate the migration**

Run: `uv run python manage.py makemigrations chat`
Expected: creates `apps/chat/migrations/0XXX_threadjob.py`. Inspect it to confirm only the `ThreadJob` table is added.

- [ ] **Step 5: Run the test to verify it passes**

Run: `uv run pytest tests/test_threadjob_model.py -v`
Expected: PASS, both tests.

- [ ] **Step 6: Commit**

```bash
git add apps/chat/models.py apps/chat/migrations/0XXX_threadjob.py tests/test_threadjob_model.py
git commit -m "feat(chat): add ThreadJob model for background job tracking"
```

---

### Task 2: `Thread.last_viewed_at`

**Files:**
- Modify: `apps/chat/models.py`
- Create: `apps/chat/migrations/0XXX_thread_last_viewed_at.py` (generated)
- Test: extend `tests/test_threadjob_model.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_threadjob_model.py`:

```python
@pytest.mark.django_db
def test_thread_last_viewed_at_defaults_to_null():
    user = User.objects.create_user(email="a@b.c", password="x")
    ws = Workspace.objects.create(name="W", created_by=user)
    thread = Thread.objects.create(workspace=ws, user=user)
    assert thread.last_viewed_at is None
```

- [ ] **Step 2: Run test, verify it fails**

Run: `uv run pytest tests/test_threadjob_model.py::test_thread_last_viewed_at_defaults_to_null -v`
Expected: FAIL — `Thread` has no `last_viewed_at`.

- [ ] **Step 3: Add the field**

In `apps/chat/models.py`, inside `Thread`, after the existing `updated_at` field:

```python
    last_viewed_at = models.DateTimeField(null=True, blank=True)
```

- [ ] **Step 4: Generate the migration**

Run: `uv run python manage.py makemigrations chat`

- [ ] **Step 5: Run test, verify it passes**

Run: `uv run pytest tests/test_threadjob_model.py::test_thread_last_viewed_at_defaults_to_null -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add apps/chat/models.py apps/chat/migrations/0XXX_thread_last_viewed_at.py tests/test_threadjob_model.py
git commit -m "feat(chat): add Thread.last_viewed_at for unread-tracking"
```

---

## Phase 2 — Backend status & cancel endpoints

### Task 3: `GET /api/workspaces/<id>/jobs/active/`

**Files:**
- Create: `apps/workspaces/api/jobs_views.py`
- Modify: `apps/workspaces/api/urls.py`
- Test: `tests/test_jobs_endpoints.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_jobs_endpoints.py
import pytest
from asgiref.sync import sync_to_async
from django.test import AsyncClient
from apps.chat.models import Thread, ThreadJob
from apps.workspaces.models import (
    MaterializationRun, Tenant, TenantSchema, WorkspaceTenant, Workspace,
)
from django.contrib.auth import get_user_model

User = get_user_model()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_active_jobs_returns_pending_job_with_progress():
    user = await sync_to_async(User.objects.create_user)(email="a@b.c", password="x")
    ws = await sync_to_async(Workspace.objects.create)(name="W", created_by=user)
    tenant = await sync_to_async(Tenant.objects.create)(
        external_id="t1", provider="commcare",
    )
    await sync_to_async(WorkspaceTenant.objects.create)(workspace=ws, tenant=tenant)
    schema = await sync_to_async(TenantSchema.objects.create)(
        tenant=tenant, schema_name="s_t1",
    )
    thread = await sync_to_async(Thread.objects.create)(workspace=ws, user=user)
    await sync_to_async(ThreadJob.objects.create)(
        thread=thread, job_type="materialization",
        procrastinate_job_id=1001, tool_call_id="tc1",
    )
    await sync_to_async(MaterializationRun.objects.create)(
        tenant_schema=schema, pipeline="commcare_sync",
        state=MaterializationRun.RunState.LOADING,
        procrastinate_job_id=1001,
        progress={
            "step": 3, "total_steps": 5,
            "source": "cases", "message": "Loading cases...",
            "rows_loaded": 64000, "rows_total": 100000,
            "run_id": str(schema.id),
        },
    )
    client = AsyncClient()
    await sync_to_async(client.login)(email="a@b.c", password="x")
    resp = await client.get(f"/api/workspaces/{ws.id}/jobs/active/")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["jobs"]) == 1
    j = body["jobs"][0]
    assert j["thread_id"] == str(thread.id)
    assert j["state"] == "pending"
    assert j["progress"]["percent"] == 64
    assert j["progress"]["rows_loaded"] == 64000


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_active_jobs_empty_when_none_running():
    user = await sync_to_async(User.objects.create_user)(email="a@b.c", password="x")
    ws = await sync_to_async(Workspace.objects.create)(name="W", created_by=user)
    client = AsyncClient()
    await sync_to_async(client.login)(email="a@b.c", password="x")
    resp = await client.get(f"/api/workspaces/{ws.id}/jobs/active/")
    assert resp.status_code == 200
    assert resp.json() == {"jobs": []}
```

- [ ] **Step 2: Run test, verify it fails**

Run: `uv run pytest tests/test_jobs_endpoints.py -v`
Expected: FAIL — URL not found.

- [ ] **Step 3: Implement the view**

Create `apps/workspaces/api/jobs_views.py`:

```python
"""Async API views for ThreadJob status (polled by the frontend)."""

import logging

from django.http import JsonResponse

from apps.chat.models import ThreadJob
from apps.users.decorators import async_login_required
from apps.workspaces.models import MaterializationRun
from apps.workspaces.workspace_resolver import aresolve_workspace

logger = logging.getLogger(__name__)


def _job_to_dict(job: ThreadJob, run_progress: dict | None) -> dict:
    progress = None
    if run_progress:
        rows_loaded = run_progress.get("rows_loaded") or 0
        rows_total = run_progress.get("rows_total")
        percent = None
        if isinstance(rows_total, int) and rows_total > 0:
            percent = int(100 * rows_loaded / rows_total)
        progress = {
            "percent": percent,
            "rows_loaded": rows_loaded,
            "rows_total": rows_total,
            "message": run_progress.get("message"),
            "source": run_progress.get("source"),
            "step": run_progress.get("step"),
            "total_steps": run_progress.get("total_steps"),
        }
    return {
        "thread_job_id": str(job.id),
        "thread_id": str(job.thread_id),
        "job_type": job.job_type,
        "state": job.state,
        "progress": progress,
        "created_at": job.created_at.isoformat(),
    }


@async_login_required
async def active_jobs_view(request, workspace_id):
    """GET /api/workspaces/<workspace_id>/jobs/active/

    Returns ThreadJobs in non-terminal states for the current user, enriched
    with the latest MaterializationRun.progress. Polled by useWorkspaceJobs.
    """
    if request.method != "GET":
        return JsonResponse({"error": "Method not allowed"}, status=405)

    user = request._authenticated_user
    workspace, err = await aresolve_workspace(user, workspace_id)
    if err is not None:
        return err

    # ThreadJobs for this user's threads in this workspace, in active states.
    jobs = [
        j
        async for j in ThreadJob.objects.select_related("thread").filter(
            thread__workspace=workspace,
            thread__user=user,
            state__in=list(ThreadJob.ACTIVE_STATES),
        ).order_by("-created_at")
    ]

    # Bulk-fetch the latest progress per procrastinate_job_id.
    job_ids = [j.procrastinate_job_id for j in jobs]
    runs_by_job: dict[int, dict] = {}
    async for r in MaterializationRun.objects.filter(
        procrastinate_job_id__in=job_ids,
    ).order_by("started_at"):
        # Last wins; per workspace this is "the currently active tenant_schema run".
        runs_by_job[r.procrastinate_job_id] = r.progress or {}

    return JsonResponse(
        {"jobs": [_job_to_dict(j, runs_by_job.get(j.procrastinate_job_id)) for j in jobs]}
    )
```

- [ ] **Step 4: Wire the URL**

In `apps/workspaces/api/urls.py`, add the import and URL:

```python
from .jobs_views import active_jobs_view
# ... within urlpatterns:
    path("<uuid:workspace_id>/jobs/active/", active_jobs_view, name="active_jobs"),
```

- [ ] **Step 5: Run test, verify it passes**

Run: `uv run pytest tests/test_jobs_endpoints.py -v`
Expected: PASS, both tests.

- [ ] **Step 6: Commit**

```bash
git add apps/workspaces/api/jobs_views.py apps/workspaces/api/urls.py tests/test_jobs_endpoints.py
git commit -m "feat(workspaces): add GET /jobs/active/ endpoint for ThreadJob status"
```

---

### Task 4: `POST /api/workspaces/<id>/jobs/<thread_job_id>/cancel/`

**Files:**
- Create: `apps/workspaces/api/jobs_cancel.py` (new helper module — shared cancel logic)
- Modify: `apps/workspaces/api/jobs_views.py` (add cancel view)
- Modify: `apps/workspaces/api/materialization_views.py` (delegate to shared logic)
- Modify: `apps/workspaces/api/urls.py`
- Test: extend `tests/test_jobs_endpoints.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_jobs_endpoints.py`:

```python
from unittest.mock import patch, AsyncMock


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_cancel_job_flips_state_and_aborts_procrastinate():
    user = await sync_to_async(User.objects.create_user)(email="a@b.c", password="x")
    ws = await sync_to_async(Workspace.objects.create)(name="W", created_by=user)
    tenant = await sync_to_async(Tenant.objects.create)(external_id="t1", provider="commcare")
    await sync_to_async(WorkspaceTenant.objects.create)(workspace=ws, tenant=tenant)
    schema = await sync_to_async(TenantSchema.objects.create)(tenant=tenant, schema_name="s_t1")
    thread = await sync_to_async(Thread.objects.create)(workspace=ws, user=user)
    tj = await sync_to_async(ThreadJob.objects.create)(
        thread=thread, job_type="materialization",
        procrastinate_job_id=2002, tool_call_id="tc2",
        state=ThreadJob.State.RUNNING,
    )
    await sync_to_async(MaterializationRun.objects.create)(
        tenant_schema=schema, pipeline="commcare_sync",
        state=MaterializationRun.RunState.LOADING,
        procrastinate_job_id=2002,
    )
    client = AsyncClient()
    await sync_to_async(client.login)(email="a@b.c", password="x")

    with patch(
        "apps.workspaces.api.jobs_cancel.current_app"
    ) as mock_app:
        mock_app.job_manager.cancel_job_by_id_async = AsyncMock(return_value=None)
        resp = await client.post(f"/api/workspaces/{ws.id}/jobs/{tj.id}/cancel/")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "cancelled"
    await sync_to_async(tj.refresh_from_db)()
    assert tj.state == ThreadJob.State.CANCELLED
    run = await MaterializationRun.objects.aget(procrastinate_job_id=2002)
    assert run.state == MaterializationRun.RunState.CANCELLED
```

- [ ] **Step 2: Run test, verify it fails**

Run: `uv run pytest tests/test_jobs_endpoints.py::test_cancel_job_flips_state_and_aborts_procrastinate -v`
Expected: FAIL — URL not found.

- [ ] **Step 3: Create the shared cancel helper**

Create `apps/workspaces/api/jobs_cancel.py`:

```python
"""Shared cancel logic for ThreadJob + the materialization runs it owns.

Both the per-job cancel endpoint and the legacy materialization cancel endpoint
funnel through ``cancel_thread_job`` so the order-of-operations (DB state flip
BEFORE procrastinate abort) stays correct.
"""

import logging
from datetime import UTC, datetime

from procrastinate.contrib.django.procrastinate_app import current_app

from apps.chat.models import ThreadJob
from apps.workspaces.models import MaterializationRun

logger = logging.getLogger(__name__)


async def cancel_thread_job(thread_job: ThreadJob) -> int:
    """Cancel the given ThreadJob and its associated MaterializationRuns.

    Returns the number of MaterializationRun rows flipped to CANCELLED.

    Order matters: DB state is flipped before the procrastinate abort signal,
    because the worker's progress_updater checks DB state on every page and
    procrastinate's abort only fires at the next ``await`` boundary.
    """
    now = datetime.now(UTC)

    run_ids = [
        r.id
        async for r in MaterializationRun.objects.filter(
            procrastinate_job_id=thread_job.procrastinate_job_id,
            state__in=list(MaterializationRun.ACTIVE_STATES),
        )
    ]
    runs_cancelled = 0
    if run_ids:
        runs_cancelled = await MaterializationRun.objects.filter(id__in=run_ids).aupdate(
            state=MaterializationRun.RunState.CANCELLED, completed_at=now,
        )

    await ThreadJob.objects.filter(id=thread_job.id).aupdate(
        state=ThreadJob.State.CANCELLED, completed_at=now,
    )

    try:
        await current_app.job_manager.cancel_job_by_id_async(
            thread_job.procrastinate_job_id, abort=True
        )
    except Exception:
        logger.warning(
            "Failed to abort procrastinate job %s", thread_job.procrastinate_job_id,
            exc_info=True,
        )

    return runs_cancelled
```

- [ ] **Step 4: Add cancel view**

Append to `apps/workspaces/api/jobs_views.py`:

```python
from uuid import UUID
from apps.workspaces.api.jobs_cancel import cancel_thread_job


@async_login_required
async def cancel_job_view(request, workspace_id, thread_job_id):
    """POST /api/workspaces/<workspace_id>/jobs/<thread_job_id>/cancel/"""
    if request.method != "POST":
        return JsonResponse({"error": "Method not allowed"}, status=405)

    user = request._authenticated_user
    workspace, err = await aresolve_workspace(user, workspace_id)
    if err is not None:
        return err

    try:
        tj = await ThreadJob.objects.select_related("thread").aget(
            id=thread_job_id,
            thread__workspace=workspace,
            thread__user=user,
        )
    except ThreadJob.DoesNotExist:
        return JsonResponse({"error": "ThreadJob not found"}, status=404)

    if tj.state in ThreadJob.TERMINAL_STATES:
        return JsonResponse({"status": "already_terminal", "state": tj.state})

    runs_cancelled = await cancel_thread_job(tj)
    return JsonResponse({"status": "cancelled", "runs_cancelled": runs_cancelled})
```

- [ ] **Step 5: Wire URL**

In `apps/workspaces/api/urls.py`, add:

```python
from .jobs_views import active_jobs_view, cancel_job_view
# urlpatterns:
    path(
        "<uuid:workspace_id>/jobs/<uuid:thread_job_id>/cancel/",
        cancel_job_view, name="cancel_job",
    ),
```

- [ ] **Step 6: Run test, verify it passes**

Run: `uv run pytest tests/test_jobs_endpoints.py::test_cancel_job_flips_state_and_aborts_procrastinate -v`
Expected: PASS.

- [ ] **Step 7: Refactor legacy materialization cancel to delegate**

Update `apps/workspaces/api/materialization_views.py` so it calls `cancel_thread_job` for each `ThreadJob` whose `procrastinate_job_id` matches one of the active runs in the workspace. This preserves the legacy URL but unifies the implementation.

Replace the body of `materialization_cancel_view` after the auth checks with:

```python
    active_runs = [
        r
        async for r in MaterializationRun.objects.select_related(
            "tenant_schema__tenant"
        ).filter(
            tenant_schema__tenant__in=workspace.tenants.all(),
            state__in=list(MaterializationRun.ACTIVE_STATES),
        )
    ]
    if not active_runs:
        return JsonResponse({"status": "no_active_run", "runs_cancelled": 0})

    job_ids = {r.procrastinate_job_id for r in active_runs if r.procrastinate_job_id is not None}
    tjs = [
        tj
        async for tj in ThreadJob.objects.filter(procrastinate_job_id__in=job_ids)
    ]
    total = 0
    for tj in tjs:
        total += await cancel_thread_job(tj)
    return JsonResponse({"status": "cancelled", "runs_cancelled": total})
```

Add `from apps.workspaces.api.jobs_cancel import cancel_thread_job` at the top.

- [ ] **Step 8: Run the existing materialization-cancel tests**

Run: `uv run pytest tests/ -k cancel -v`
Expected: existing cancel tests still pass. (Update any tests that asserted the legacy behavior — there should be at most one.)

- [ ] **Step 9: Commit**

```bash
git add apps/workspaces/api/jobs_views.py apps/workspaces/api/jobs_cancel.py apps/workspaces/api/materialization_views.py apps/workspaces/api/urls.py tests/test_jobs_endpoints.py
git commit -m "feat(workspaces): add POST /jobs/<id>/cancel/ + unify cancel logic"
```

---

### Task 5: `POST /api/chat/threads/<id>/viewed/`

**Files:**
- Modify: `apps/chat/thread_views.py`
- Modify: `apps/chat/urls.py`
- Test: `tests/test_thread_viewed_endpoint.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_thread_viewed_endpoint.py
import pytest
from asgiref.sync import sync_to_async
from django.test import AsyncClient
from apps.chat.models import Thread
from apps.workspaces.models import Workspace
from django.contrib.auth import get_user_model

User = get_user_model()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_thread_viewed_sets_last_viewed_at():
    user = await sync_to_async(User.objects.create_user)(email="a@b.c", password="x")
    ws = await sync_to_async(Workspace.objects.create)(name="W", created_by=user)
    thread = await sync_to_async(Thread.objects.create)(workspace=ws, user=user)
    client = AsyncClient()
    await sync_to_async(client.login)(email="a@b.c", password="x")

    resp = await client.post(
        f"/api/workspaces/{ws.id}/threads/{thread.id}/viewed/"
    )
    assert resp.status_code == 200
    await sync_to_async(thread.refresh_from_db)()
    assert thread.last_viewed_at is not None
```

- [ ] **Step 2: Run test, verify it fails**

Run: `uv run pytest tests/test_thread_viewed_endpoint.py -v`
Expected: FAIL — URL not found.

- [ ] **Step 3: Implement the view**

In `apps/chat/thread_views.py`, append:

```python
from datetime import UTC, datetime


@async_login_required
async def thread_viewed_view(request, workspace_id, thread_id):
    """POST /api/workspaces/<workspace_id>/threads/<thread_id>/viewed/

    Update Thread.last_viewed_at to now. Called by the frontend when the user
    opens a thread; clears the green-dot unread indicator.
    """
    if request.method != "POST":
        return JsonResponse({"error": "Method not allowed"}, status=405)

    user = request._authenticated_user
    workspace, _, _ = await _resolve_workspace_and_membership(user, workspace_id)
    if workspace is None:
        return JsonResponse({"error": "Workspace not found or access denied"}, status=403)

    updated = await Thread.objects.filter(
        id=thread_id, user=user, workspace=workspace,
    ).aupdate(last_viewed_at=datetime.now(UTC))
    if not updated:
        return JsonResponse({"error": "Thread not found"}, status=404)
    return JsonResponse({"status": "ok"})
```

- [ ] **Step 4: Wire URL**

In `apps/chat/urls.py`, add the import and pattern:

```python
from apps.chat.thread_views import thread_viewed_view
# urlpatterns:
    path(
        "<uuid:workspace_id>/threads/<uuid:thread_id>/viewed/",
        thread_viewed_view, name="thread_viewed",
    ),
```

- [ ] **Step 5: Run test, verify it passes**

Run: `uv run pytest tests/test_thread_viewed_endpoint.py -v`
Expected: PASS.

- [ ] **Step 6: Update thread list response to include `last_viewed_at`**

In `apps/chat/thread_views.py`, update `_list_threads` to include the field:

```python
        {
            "id": str(t.id),
            "title": t.title,
            "created_at": t.created_at.isoformat(),
            "updated_at": t.updated_at.isoformat(),
            "is_shared": t.is_shared,
            "last_viewed_at": t.last_viewed_at.isoformat() if t.last_viewed_at else None,
        }
```

- [ ] **Step 7: Commit**

```bash
git add apps/chat/thread_views.py apps/chat/urls.py tests/test_thread_viewed_endpoint.py
git commit -m "feat(chat): add POST /threads/<id>/viewed/ + last_viewed_at in list"
```

---

## Phase 3 — Backend: MCP tool & agent graph

### Task 6: Inject `thread_id` into MCP tool calls

**Files:**
- Modify: `apps/agents/graph/state.py` (add `thread_id` to `AgentState`)
- Modify: `apps/agents/graph/base.py` (extend `injections`, accept thread_id input)
- Modify: `apps/chat/views.py` (pass thread_id into input_state)
- Test: `tests/test_agent_graph_injection.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_agent_graph_injection.py
import pytest
from apps.agents.graph.state import AgentState


def test_agent_state_has_thread_id_field():
    # TypedDict membership check.
    assert "thread_id" in AgentState.__annotations__
```

- [ ] **Step 2: Run test, verify it fails**

Run: `uv run pytest tests/test_agent_graph_injection.py -v`
Expected: FAIL.

- [ ] **Step 3: Add `thread_id` to `AgentState`**

In `apps/agents/graph/state.py`, inside `class AgentState(TypedDict)`:

```python
    thread_id: str
```

Also update the docstring block describing the fields to add `thread_id`:

> ``thread_id`` : str
>     UUID of the active chat thread (as string). Injected into MCP tool calls
>     that need to associate background jobs (ThreadJob) with the conversation.

- [ ] **Step 4: Extend injections dict in `build_agent_graph`**

In `apps/agents/graph/base.py:336`, change:

```python
    injections = {"workspace_id": "workspace_id", "user_id": "user_id"}
```

to:

```python
    injections = {
        "workspace_id": "workspace_id",
        "user_id": "user_id",
        "_thread_id": "thread_id",
    }
```

- [ ] **Step 5: Inject `_tool_call_id` per-call in `_make_injecting_tool_node`**

Replace the inner loop in `_make_injecting_tool_node` (around line 299) with one that adds `_tool_call_id` from the LangChain tool_call:

```python
            for tc in last_msg.tool_calls:
                if tc["name"] in MCP_TOOL_NAMES:
                    extra = {k: state.get(v, "") for k, v in injections.items()}
                    extra["_tool_call_id"] = tc.get("id", "")
                    tc = {**tc, "args": {**tc["args"], **extra}}
                modified_calls.append(tc)
```

Update `hidden_params` to include the new injected arg name so the LLM doesn't see them in the schema:

```python
    hidden_params = [*injections.keys(), "_tool_call_id"]
```

- [ ] **Step 6: Plumb `thread_id` from chat views**

In `apps/chat/views.py:204`, update `input_state`:

```python
    input_state = {
        "messages": [*dangling_tool_results, HumanMessage(content=user_content)],
        "workspace_id": str(workspace.id),
        "user_id": str(user.id),
        "user_role": "analyst",
        "thread_id": str(thread_id),
    }
```

- [ ] **Step 7: Run tests**

Run: `uv run pytest tests/test_agent_graph_injection.py tests/test_agent_graph.py -v`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add apps/agents/graph/state.py apps/agents/graph/base.py apps/chat/views.py tests/test_agent_graph_injection.py
git commit -m "feat(agents): inject thread_id + tool_call_id into MCP tool calls"
```

---

### Task 7: Refactor `run_materialization` to fire-and-acknowledge

**Files:**
- Modify: `mcp_server/server.py`
- Test: `tests/test_run_materialization_fire_and_ack.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_run_materialization_fire_and_ack.py
import pytest
from asgiref.sync import sync_to_async
from unittest.mock import patch, AsyncMock, MagicMock
from apps.chat.models import Thread, ThreadJob
from apps.workspaces.models import (
    Tenant, TenantMembership, WorkspaceTenant, Workspace,
)
from django.contrib.auth import get_user_model

User = get_user_model()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_run_materialization_returns_started_immediately_and_creates_threadjob():
    user = await sync_to_async(User.objects.create_user)(email="a@b.c", password="x")
    ws = await sync_to_async(Workspace.objects.create)(name="W", created_by=user)
    tenant = await sync_to_async(Tenant.objects.create)(external_id="t1", provider="commcare")
    await sync_to_async(WorkspaceTenant.objects.create)(workspace=ws, tenant=tenant)
    await sync_to_async(TenantMembership.objects.create)(tenant=tenant, user=user)
    thread = await sync_to_async(Thread.objects.create)(workspace=ws, user=user)

    from mcp_server.server import run_materialization

    job_mock = MagicMock(id=7777)
    with patch("mcp_server.server.materialize_workspace") as mw:
        mw.defer_async = AsyncMock(return_value=job_mock)
        result = await run_materialization(
            workspace_id=str(ws.id),
            user_id=str(user.id),
            _thread_id=str(thread.id),
            _tool_call_id="tc-xyz",
        )

    assert result["data"]["status"] == "started"
    assert "thread_job_id" in result["data"]
    tj = await ThreadJob.objects.aget(procrastinate_job_id=7777)
    assert tj.thread_id == thread.id
    assert tj.tool_call_id == "tc-xyz"
    assert tj.state == ThreadJob.State.PENDING
```

- [ ] **Step 2: Run test, verify it fails**

Run: `uv run pytest tests/test_run_materialization_fire_and_ack.py -v`
Expected: FAIL — `run_materialization` does not accept `_thread_id`/`_tool_call_id`.

- [ ] **Step 3: Rewrite `run_materialization`**

Replace `mcp_server/server.py` lines around the current `@mcp.tool() async def run_materialization(...)` with:

```python
@mcp.tool()
async def run_materialization(
    workspace_id: str = "",
    user_id: str = "",
    _thread_id: str = "",
    _tool_call_id: str = "",
    ctx: Context | None = None,
) -> dict:
    """Start a materialization in the background and acknowledge immediately.

    Defers the work to the procrastinate ``materialize_workspace`` task and
    creates a ThreadJob row tying that procrastinate job to the calling chat
    thread. Returns ``status: started`` right away — the chat agent should
    acknowledge briefly to the user and end its turn. When materialization
    finishes, a chained ``resume_thread_after_materialization`` task injects
    completion into the conversation via the LangGraph checkpointer.

    Args:
        workspace_id: Workspace UUID (injected server-side by the agent graph).
        user_id: User UUID (injected server-side).
        _thread_id: Chat thread UUID (injected server-side).
        _tool_call_id: LangChain tool_call_id for this invocation (injected
            server-side); persisted on ThreadJob so the resume task can
            attribute its work to the right call.
    """
    async with tool_context("run_materialization", workspace_id) as tc:
        if not workspace_id:
            tc["result"] = error_response(VALIDATION_ERROR, "workspace_id is required")
            return tc["result"]
        if not _thread_id:
            tc["result"] = error_response(VALIDATION_ERROR, "_thread_id is required")
            return tc["result"]

        memberships, err = await _resolve_workspace_memberships(workspace_id, user_id)
        if err:
            tc["result"] = error_response(NOT_FOUND, err)
            return tc["result"]

        try:
            job = await materialize_workspace.defer_async(
                workspace_id=str(workspace_id),
                user_id=str(user_id) if user_id else "",
            )
        except Exception:
            logger.exception("Failed to dispatch materialize_workspace task")
            tc["result"] = error_response(
                INTERNAL_ERROR, "Failed to dispatch materialization task"
            )
            return tc["result"]
        job_id = getattr(job, "id", job) if not isinstance(job, int) else job

        try:
            tj = await ThreadJob.objects.acreate(
                thread_id=_thread_id,
                job_type=ThreadJob.JobType.MATERIALIZATION,
                procrastinate_job_id=job_id,
                tool_call_id=_tool_call_id,
                state=ThreadJob.State.PENDING,
            )
        except Exception:
            logger.exception("Failed to create ThreadJob; rolling back dispatch")
            try:
                await _procrastinate_app.job_manager.cancel_job_by_id_async(job_id, abort=True)
            except Exception:
                pass
            tc["result"] = error_response(INTERNAL_ERROR, "Failed to track job")
            return tc["result"]

        tc["result"] = success_response(
            {
                "status": "started",
                "thread_job_id": str(tj.id),
                "message": (
                    "Materialization started in background. "
                    "I'll continue when it finishes."
                ),
            },
            schema="",
            timing_ms=tc["timer"].elapsed_ms,
        )
        return tc["result"]
```

Add at the top of `mcp_server/server.py` if not already imported:

```python
from apps.chat.models import ThreadJob
```

- [ ] **Step 4: Run test, verify it passes**

Run: `uv run pytest tests/test_run_materialization_fire_and_ack.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add mcp_server/server.py tests/test_run_materialization_fire_and_ack.py
git commit -m "feat(mcp): run_materialization fires-and-acknowledges via ThreadJob"
```

---

### Task 8: Delete the old polling code

**Files:**
- Modify: `mcp_server/server.py`

- [ ] **Step 1: Remove constants and helpers**

Delete from `mcp_server/server.py`:
- `_MATERIALIZATION_POLL_INTERVAL_S` and `_MATERIALIZATION_POLL_DEADLINE_S` constants (around line 512).
- `_format_progress_message` function.
- `_is_procrastinate_job_finished` function.
- `_query_workspace_progress` function.
- `_PROCRASTINATE_TERMINAL_STATUSES` constant (if defined nearby).

Look at the existing functions and confirm nothing else imports them. Run:

```bash
grep -rn "_MATERIALIZATION_POLL\|_format_progress_message\|_query_workspace_progress\|_is_procrastinate_job_finished" mcp_server/ apps/ tests/
```

Update any test files that reference them — `tests/test_run_materialization_progress.py` is likely dead and should be deleted entirely. Confirm by reading it and verifying it tests only the now-gone poll loop.

- [ ] **Step 2: Run the full mcp_server test suite**

Run: `uv run pytest tests/ -k mcp_server -v` and `uv run pytest tests/test_run_materialization* -v`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add mcp_server/server.py tests/
git commit -m "refactor(mcp): remove poll loop helpers (superseded by ThreadJob)"
```

---

### Task 9: Delete `progress_queue` plumbing in chat layer

**Files:**
- Modify: `apps/chat/views.py`
- Modify: `apps/chat/stream.py`
- Modify: `apps/agents/mcp_client.py` (drop `on_progress` arg)

- [ ] **Step 1: Strip the progress callback plumbing in `apps/chat/views.py`**

Remove around line 137:

```python
    progress_queue: asyncio.Queue = asyncio.Queue()

    async def _on_mcp_progress(progress, total, message, context) -> None:
        if message is not None:
            await progress_queue.put(...)
```

Change the `get_mcp_tools` call:

```python
    mcp_tools = await get_mcp_tools()
```

Remove `progress_queue=progress_queue` from the `langgraph_to_ui_stream` call (line 234) so it becomes:

```python
            async for chunk in langgraph_to_ui_stream(agent, input_state, config):
                yield chunk
```

- [ ] **Step 2: Strip `progress_queue` from `apps/chat/stream.py`**

In `apps/chat/stream.py`:
- Remove the `progress_queue: asyncio.Queue | None = None` parameter from `langgraph_to_ui_stream`.
- Remove the `_queue = ...`, `pg_task = ...`, all `pg_task` handling, and the "drain progress items" loop on `on_tool_end`.
- Remove `_PROGRESS_TOOLS` set.
- Remove the `on_tool_start` early-card-open branch (we no longer need it; cards open on `on_tool_end` like every other tool).
- Keep the `AGENT_TIMEOUT_SECONDS = 300` ceiling — it now ONLY guards genuinely slow LLM work, which it should.

The simplified body of the main loop:

```python
    try:
        deadline = asyncio.get_event_loop().time() + AGENT_TIMEOUT_SECONDS

        while True:
            if asyncio.get_event_loop().time() > deadline:
                raise TimeoutError(f"Agent execution exceeded {AGENT_TIMEOUT_SECONDS}s timeout")
            try:
                event = await event_stream.__anext__()
            except StopAsyncIteration:
                break

            event_type = event.get("event")
            # ... (keep existing on_chat_model_stream / on_tool_end branches)
```

- [ ] **Step 3: Drop `on_progress` from `apps/agents/mcp_client.py`**

In `apps/agents/mcp_client.py:32`, simplify the signature:

```python
async def get_mcp_tools() -> list:
    """Build MCP tools list."""
    # remove the callbacks plumbing
```

Remove the `ProgressCallback` import and `Callbacks` construction.

- [ ] **Step 4: Run existing chat stream tests**

Run: `uv run pytest tests/ -k chat -v`
Expected: PASS. Update any tests that exercised the deleted progress path.

- [ ] **Step 5: Commit**

```bash
git add apps/chat/views.py apps/chat/stream.py apps/agents/mcp_client.py tests/
git commit -m "refactor(chat): drop progress_queue plumbing (ThreadJob replaces it)"
```

---

### Task 10: Update agent prompt for `run_materialization`

**Files:**
- Modify: `apps/agents/prompts/system_prompt.py` (or wherever the materialization guidance lives — locate at impl time with `grep -rn "run_materialization" apps/agents/prompts/`)

- [ ] **Step 1: Locate the existing materialization prompt section**

Run: `grep -rn "run_materialization" apps/agents/prompts/`
Find the file describing tool usage. Likely `apps/agents/prompts/system_prompt.py`.

- [ ] **Step 2: Update the relevant section**

Replace the existing description of `run_materialization` with:

```
- `run_materialization`: starts a background materialization job and returns
  IMMEDIATELY with status="started". On seeing status="started", acknowledge
  briefly to the user in ONE sentence (e.g., "I've started loading the data,
  I'll continue once it finishes") and END YOUR TURN. Do NOT call other data
  tools (query, describe_table, etc.) in the same turn — the data is not yet
  available. The system will resume the conversation automatically when
  materialization completes.
```

- [ ] **Step 3: Commit**

```bash
git add apps/agents/prompts/
git commit -m "docs(agents): update run_materialization prompt for fire-and-ack"
```

---

## Phase 4 — Backend: resume + janitor

### Task 11: Implement `resume_thread_after_materialization`

**Files:**
- Modify: `apps/workspaces/tasks.py`
- Test: `tests/test_resume_thread_task.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_resume_thread_task.py
import pytest
from unittest.mock import patch, AsyncMock, MagicMock
from asgiref.sync import sync_to_async
from apps.chat.models import Thread, ThreadJob
from apps.workspaces.models import (
    MaterializationRun, Tenant, TenantSchema, WorkspaceTenant, Workspace,
)
from django.contrib.auth import get_user_model

User = get_user_model()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_resume_appends_system_message_and_invokes_agent():
    user = await sync_to_async(User.objects.create_user)(email="a@b.c", password="x")
    ws = await sync_to_async(Workspace.objects.create)(name="W", created_by=user)
    tenant = await sync_to_async(Tenant.objects.create)(external_id="t1", provider="commcare")
    await sync_to_async(WorkspaceTenant.objects.create)(workspace=ws, tenant=tenant)
    schema = await sync_to_async(TenantSchema.objects.create)(tenant=tenant, schema_name="s_t1")
    thread = await sync_to_async(Thread.objects.create)(workspace=ws, user=user)
    tj = await sync_to_async(ThreadJob.objects.create)(
        thread=thread, job_type="materialization",
        procrastinate_job_id=3003, tool_call_id="tc3",
        state=ThreadJob.State.RUNNING,
    )
    await sync_to_async(MaterializationRun.objects.create)(
        tenant_schema=schema, pipeline="commcare_sync",
        state=MaterializationRun.RunState.COMPLETED,
        procrastinate_job_id=3003,
        result={"rows": 50000},
    )

    from apps.workspaces.tasks import resume_thread_after_materialization

    mock_agent = MagicMock()
    mock_agent.ainvoke = AsyncMock(return_value={"messages": []})
    with patch("apps.workspaces.tasks._build_agent_for_resume", AsyncMock(return_value=mock_agent)):
        # The task takes thread_job_id; we call it directly (not via procrastinate).
        result = await resume_thread_after_materialization(
            None, thread_job_id=str(tj.id),
        )

    assert result["status"] == "resumed"
    await sync_to_async(tj.refresh_from_db)()
    assert tj.state == ThreadJob.State.COMPLETED
    # Inspect the input_state passed to ainvoke.
    call_args = mock_agent.ainvoke.await_args
    input_state = call_args.args[0]
    messages = input_state["messages"]
    assert len(messages) == 1
    assert messages[0].content.startswith("[__system_resume__]")
    assert "completed" in messages[0].content
```

- [ ] **Step 2: Run test, verify it fails**

Run: `uv run pytest tests/test_resume_thread_task.py -v`
Expected: FAIL — function not defined.

- [ ] **Step 3: Implement the task**

Append to `apps/workspaces/tasks.py`:

```python
from apps.chat.models import ThreadJob


SYSTEM_RESUME_MARKER = "[__system_resume__]"


async def _build_agent_for_resume(workspace, user):
    """Build the LangGraph agent in the same shape views.py does, minus streaming."""
    from apps.agents.graph.base import build_agent_graph
    from apps.agents.mcp_client import get_mcp_tools, get_user_oauth_tokens
    from apps.chat.checkpointer import ensure_checkpointer

    mcp_tools = await get_mcp_tools()
    oauth_tokens = await get_user_oauth_tokens(user)
    checkpointer = await ensure_checkpointer()
    return await build_agent_graph(
        workspace=workspace,
        user=user,
        checkpointer=checkpointer,
        mcp_tools=mcp_tools,
        oauth_tokens=oauth_tokens,
    )


async def _aggregate_materialization_state(procrastinate_job_id: int) -> tuple[str, list[dict]]:
    """Inspect MaterializationRun rows for this job, return (status, per-tenant summary)."""
    runs = [
        r
        async for r in MaterializationRun.objects.filter(
            procrastinate_job_id=procrastinate_job_id,
        ).select_related("tenant_schema__tenant")
    ]
    if not runs:
        return "no_runs", []
    summary: list[dict] = []
    any_cancelled = False
    any_failed = False
    all_completed = True
    for r in runs:
        tenant_id = r.tenant_schema.tenant.external_id
        summary.append({"tenant": tenant_id, "state": r.state, "result": r.result})
        if r.state == MaterializationRun.RunState.CANCELLED:
            any_cancelled = True
            all_completed = False
        elif r.state == MaterializationRun.RunState.FAILED:
            any_failed = True
            all_completed = False
        elif r.state != MaterializationRun.RunState.COMPLETED:
            all_completed = False
    status = (
        "cancelled" if any_cancelled
        else ("failed" if any_failed else ("completed" if all_completed else "partial"))
    )
    return status, summary


@app.task(pass_context=True)
async def resume_thread_after_materialization(context, thread_job_id: str) -> dict:
    """Inject a system-framed message into the LangGraph conversation and
    re-invoke the agent so it can respond to the original request with the
    now-loaded data.
    """
    from langchain_core.messages import HumanMessage

    try:
        tj = await ThreadJob.objects.select_related("thread__workspace", "thread__user").aget(
            id=thread_job_id
        )
    except ThreadJob.DoesNotExist:
        logger.warning("resume: ThreadJob %s not found", thread_job_id)
        return {"status": "missing"}

    if tj.state in ThreadJob.TERMINAL_STATES and tj.state != ThreadJob.State.CANCELLED:
        # Already resumed (idempotent retry); cancellation still gets one resume.
        return {"status": "already_terminal", "state": tj.state}

    status, summary = await _aggregate_materialization_state(tj.procrastinate_job_id)
    # If cancel beat us here, prefer that signal.
    if tj.state == ThreadJob.State.CANCELLED:
        status = "cancelled"

    body = (
        f"{SYSTEM_RESUME_MARKER} Materialization just completed "
        f"(status={status}). Please continue with the user's original request "
        f"using the now-loaded data. Per-tenant: {summary}"
    )

    workspace = tj.thread.workspace
    user = tj.thread.user
    agent = await _build_agent_for_resume(workspace, user)
    input_state = {
        "messages": [HumanMessage(content=body)],
        "workspace_id": str(workspace.id),
        "user_id": str(user.id),
        "user_role": "analyst",
        "thread_id": str(tj.thread.id),
    }
    config = {"configurable": {"thread_id": str(tj.thread.id)}, "recursion_limit": 50}

    try:
        await agent.ainvoke(input_state, config)
    except Exception:
        logger.exception("resume: agent.ainvoke failed for thread_job %s", thread_job_id)
        await ThreadJob.objects.filter(id=tj.id).aupdate(
            state=ThreadJob.State.FAILED,
            completed_at=timezone.now(),
        )
        return {"status": "agent_failed"}

    terminal = (
        ThreadJob.State.CANCELLED if status == "cancelled"
        else (ThreadJob.State.FAILED if status == "failed" else ThreadJob.State.COMPLETED)
    )
    await ThreadJob.objects.filter(id=tj.id).aupdate(
        state=terminal, completed_at=timezone.now(),
    )
    return {"status": "resumed", "terminal_state": terminal}
```

(`timezone` is already imported in this file; if not, add `from django.utils import timezone`.)

- [ ] **Step 4: Run test, verify it passes**

Run: `uv run pytest tests/test_resume_thread_task.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/workspaces/tasks.py tests/test_resume_thread_task.py
git commit -m "feat(workspaces): add resume_thread_after_materialization task"
```

---

### Task 12: Chain resume at end of `materialize_workspace`

**Files:**
- Modify: `apps/workspaces/tasks.py`
- Test: `tests/test_materialize_workspace_task.py` (extend)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_materialize_workspace_task.py`:

```python
@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_materialize_workspace_chains_resume_task():
    # Set up: workspace, tenant, membership, thread, ThreadJob pointing at this job.
    user = await sync_to_async(User.objects.create_user)(email="a@b.c", password="x")
    ws = await sync_to_async(Workspace.objects.create)(name="W", created_by=user)
    tenant = await sync_to_async(Tenant.objects.create)(external_id="t1", provider="commcare")
    await sync_to_async(WorkspaceTenant.objects.create)(workspace=ws, tenant=tenant)
    await sync_to_async(TenantMembership.objects.create)(tenant=tenant, user=user)
    thread = await sync_to_async(Thread.objects.create)(workspace=ws, user=user)
    tj = await sync_to_async(ThreadJob.objects.create)(
        thread=thread, job_type="materialization",
        procrastinate_job_id=4004, tool_call_id="tc4",
    )

    ctx = MagicMock()
    ctx.job.id = 4004
    with patch("apps.workspaces.tasks.resolve_credential", return_value=None), \
         patch("apps.workspaces.tasks.resume_thread_after_materialization") as resume:
        resume.defer_async = AsyncMock(return_value=MagicMock(id=5005))
        await materialize_workspace(ctx, workspace_id=str(ws.id), user_id=str(user.id))

    resume.defer_async.assert_awaited_once()
    kwargs = resume.defer_async.await_args.kwargs
    assert kwargs["thread_job_id"] == str(tj.id)
```

- [ ] **Step 2: Run test, verify it fails**

Run: `uv run pytest tests/test_materialize_workspace_task.py::test_materialize_workspace_chains_resume_task -v`
Expected: FAIL.

- [ ] **Step 3: Add the chain to `materialize_workspace`**

In `apps/workspaces/tasks.py`, at the end of `materialize_workspace` (just before `return {"tenants": ..., "all_succeeded": ...}`), add:

```python
    # Chain the resume task so the agent picks up where it left off.
    try:
        tj = await ThreadJob.objects.filter(procrastinate_job_id=job_id).afirst()
        if tj is not None:
            await resume_thread_after_materialization.defer_async(thread_job_id=str(tj.id))
    except Exception:
        logger.exception("Failed to defer resume task for job %s", job_id)
```

- [ ] **Step 4: Run test, verify it passes**

Run: `uv run pytest tests/test_materialize_workspace_task.py::test_materialize_workspace_chains_resume_task -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/workspaces/tasks.py tests/test_materialize_workspace_task.py
git commit -m "feat(workspaces): chain resume task after materialize_workspace"
```

---

### Task 13: Filter system-resume messages out of chat history

**Files:**
- Modify: `apps/chat/message_converter.py`
- Test: `tests/test_message_converter.py` (create or extend)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_message_converter.py (create if missing)
from langchain_core.messages import HumanMessage, AIMessage
from apps.chat.message_converter import langchain_messages_to_ui
from apps.workspaces.tasks import SYSTEM_RESUME_MARKER


def test_system_resume_markers_are_filtered():
    msgs = [
        HumanMessage(content="Load and analyze"),
        AIMessage(content="I've started loading."),
        HumanMessage(content=f"{SYSTEM_RESUME_MARKER} ..."),
        AIMessage(content="Done — here are the results."),
    ]
    ui = langchain_messages_to_ui(msgs)
    contents = [m.get("content") or m.get("parts") for m in ui]
    flat = str(contents)
    assert SYSTEM_RESUME_MARKER not in flat
    assert "Load and analyze" in flat
    assert "Done — here are the results." in flat
```

- [ ] **Step 2: Run test, verify it fails**

Run: `uv run pytest tests/test_message_converter.py::test_system_resume_markers_are_filtered -v`
Expected: FAIL.

- [ ] **Step 3: Add the filter**

In `apps/chat/message_converter.py`, at the top of `langchain_messages_to_ui`:

```python
def langchain_messages_to_ui(lc_messages) -> list[dict]:
    from apps.workspaces.tasks import SYSTEM_RESUME_MARKER  # avoid circular import

    visible = [
        m for m in lc_messages
        if not (
            isinstance(getattr(m, "content", None), str)
            and m.content.startswith(SYSTEM_RESUME_MARKER)
        )
    ]
    # ... existing conversion logic operates on `visible`
```

- [ ] **Step 4: Run test, verify it passes**

Run: `uv run pytest tests/test_message_converter.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/chat/message_converter.py tests/test_message_converter.py
git commit -m "feat(chat): hide system-resume marker messages from UI"
```

---

### Task 14: Janitor for orphaned `ThreadJob`s

**Files:**
- Modify: `apps/workspaces/tasks.py`
- Test: `tests/test_threadjob_janitor.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_threadjob_janitor.py
import pytest
from datetime import timedelta
from asgiref.sync import sync_to_async
from django.utils import timezone
from unittest.mock import patch, AsyncMock, MagicMock
from apps.chat.models import Thread, ThreadJob
from apps.workspaces.models import Workspace
from django.contrib.auth import get_user_model

User = get_user_model()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_janitor_flips_stale_threadjobs_to_failed():
    user = await sync_to_async(User.objects.create_user)(email="a@b.c", password="x")
    ws = await sync_to_async(Workspace.objects.create)(name="W", created_by=user)
    thread = await sync_to_async(Thread.objects.create)(workspace=ws, user=user)
    tj = await sync_to_async(ThreadJob.objects.create)(
        thread=thread, job_type="materialization",
        procrastinate_job_id=9999, tool_call_id="tc9",
        state=ThreadJob.State.PENDING,
    )
    # Backdate to before the threshold
    await ThreadJob.objects.filter(id=tj.id).aupdate(
        created_at=timezone.now() - timedelta(hours=2)
    )

    from apps.workspaces.tasks import expire_stale_thread_jobs

    with patch("apps.workspaces.tasks._procrastinate_job_active",
               new=AsyncMock(return_value=False)):
        await expire_stale_thread_jobs()

    await sync_to_async(tj.refresh_from_db)()
    assert tj.state == ThreadJob.State.FAILED
```

- [ ] **Step 2: Run test, verify it fails**

Run: `uv run pytest tests/test_threadjob_janitor.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement the janitor**

Append to `apps/workspaces/tasks.py`:

```python
from datetime import timedelta as _timedelta

STALE_JOB_THRESHOLD = _timedelta(hours=1)


async def _procrastinate_job_active(job_id: int) -> bool:
    from procrastinate.contrib.django.procrastinate_app import current_app
    try:
        status = await current_app.job_manager.get_job_status_async(job_id)
    except Exception:
        return False
    # Active statuses in procrastinate are "todo" and "doing"; everything else
    # (succeeded, failed, cancelled, aborted) is terminal.
    return status in {"todo", "doing"}


@app.periodic(cron="*/15 * * * *")
@app.task
async def expire_stale_thread_jobs(timestamp: int = 0) -> dict:
    """Flip ThreadJobs that have been active too long and whose procrastinate
    job is no longer running. Fires the resume task so the user is not stuck
    with a phantom spinner.
    """
    cutoff = timezone.now() - STALE_JOB_THRESHOLD
    flipped = 0
    async for tj in ThreadJob.objects.filter(
        state__in=list(ThreadJob.ACTIVE_STATES),
        created_at__lt=cutoff,
    ):
        if await _procrastinate_job_active(tj.procrastinate_job_id):
            continue
        await ThreadJob.objects.filter(id=tj.id).aupdate(
            state=ThreadJob.State.FAILED, completed_at=timezone.now(),
        )
        flipped += 1
        try:
            await resume_thread_after_materialization.defer_async(thread_job_id=str(tj.id))
        except Exception:
            logger.exception("Janitor: failed to defer resume for %s", tj.id)
    return {"flipped": flipped}
```

- [ ] **Step 4: Run test, verify it passes**

Run: `uv run pytest tests/test_threadjob_janitor.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/workspaces/tasks.py tests/test_threadjob_janitor.py
git commit -m "feat(workspaces): janitor expires stale ThreadJobs (15min cron)"
```

---

## Phase 5 — Frontend

### Task 15: API client (`jobs.ts`) and thread types

**Files:**
- Create: `frontend/src/api/jobs.ts`
- Modify: `frontend/src/api/threads.ts` (add `last_viewed_at` to Thread type)
- Modify: `frontend/src/api/threads.ts` (add `markViewed` fetcher)

- [ ] **Step 1: Create the jobs API module**

Create `frontend/src/api/jobs.ts`:

```typescript
import { api } from "@/api/client"

export type JobState = "pending" | "running" | "completed" | "failed" | "cancelled"

export interface JobProgress {
  percent: number | null
  rows_loaded: number
  rows_total: number | null
  message: string | null
  source: string | null
  step: number | null
  total_steps: number | null
}

export interface ActiveJob {
  thread_job_id: string
  thread_id: string
  job_type: "materialization"
  state: JobState
  progress: JobProgress | null
  created_at: string
}

export const jobsApi = {
  active: (workspaceId: string) =>
    api.get<{ jobs: ActiveJob[] }>(`/api/workspaces/${workspaceId}/jobs/active/`),
  cancel: (workspaceId: string, threadJobId: string) =>
    api.post(`/api/workspaces/${workspaceId}/jobs/${threadJobId}/cancel/`, {}),
}
```

(If `@/api/client` exposes a different fetch helper, adapt accordingly — read `frontend/src/api/` for the existing pattern first.)

- [ ] **Step 2: Update Thread type and add markViewed**

In `frontend/src/api/threads.ts`, find the `Thread` interface and add:

```typescript
  last_viewed_at: string | null
```

Add a fetcher:

```typescript
export async function markThreadViewed(workspaceId: string, threadId: string) {
  return api.post(`/api/workspaces/${workspaceId}/threads/${threadId}/viewed/`, {})
}
```

- [ ] **Step 3: Run typecheck**

Run: `cd frontend && bun run build`
Expected: build succeeds (or at least the `jobs.ts` and `threads.ts` files compile cleanly).

- [ ] **Step 4: Commit**

```bash
git add frontend/src/api/jobs.ts frontend/src/api/threads.ts
git commit -m "feat(frontend): jobs API client + Thread.last_viewed_at + markViewed"
```

---

### Task 16: `useWorkspaceJobs` polling hook

**Files:**
- Create: `frontend/src/hooks/useWorkspaceJobs.ts`

- [ ] **Step 1: Write the hook**

```typescript
// frontend/src/hooks/useWorkspaceJobs.ts
import { useEffect, useRef, useState, useCallback } from "react"
import { jobsApi, type ActiveJob } from "@/api/jobs"

const POLL_INTERVAL_MS = 3000

interface State {
  jobs: ActiveJob[]
  lastError: string | null
}

export interface UseWorkspaceJobs {
  jobs: ActiveJob[]
  jobsByThreadId: Record<string, ActiveJob>
  refresh: () => Promise<void>
  /** Call when the user just fired a tool that may have started a job;
   *  forces an immediate poll without waiting for the next tick. */
  notifyJobLikelyStarted: () => void
}

export function useWorkspaceJobs(workspaceId: string | null): UseWorkspaceJobs {
  const [state, setState] = useState<State>({ jobs: [], lastError: null })
  const onCompleteRef = useRef<((threadId: string) => void) | null>(null)
  const prevStatesRef = useRef<Map<string, string>>(new Map())

  const fetchOnce = useCallback(async () => {
    if (!workspaceId) return
    try {
      const data = await jobsApi.active(workspaceId)
      setState({ jobs: data.jobs, lastError: null })
      // Detect terminal transitions (for parent code that refetches thread messages).
      const prev = prevStatesRef.current
      const next = new Map<string, string>()
      for (const j of data.jobs) next.set(j.thread_job_id, j.state)
      for (const [id, oldState] of prev.entries()) {
        if (!next.has(id) && oldState !== "completed") {
          // Disappeared from active list → likely transitioned to terminal.
          // We don't know the thread_id anymore here; the parent should
          // refetch via the thread it knows it was watching.
        }
      }
      prevStatesRef.current = next
    } catch (e) {
      setState((s) => ({ ...s, lastError: String(e) }))
    }
  }, [workspaceId])

  useEffect(() => {
    if (!workspaceId) return
    let cancelled = false
    const interval = setInterval(() => {
      if (!cancelled) void fetchOnce()
    }, POLL_INTERVAL_MS)
    // Fire immediately on mount so the UI populates without waiting one tick.
    void fetchOnce()
    return () => {
      cancelled = true
      clearInterval(interval)
    }
  }, [workspaceId, fetchOnce])

  const jobsByThreadId = state.jobs.reduce<Record<string, ActiveJob>>((acc, j) => {
    acc[j.thread_id] = j
    return acc
  }, {})

  return {
    jobs: state.jobs,
    jobsByThreadId,
    refresh: fetchOnce,
    notifyJobLikelyStarted: fetchOnce,
  }
}
```

- [ ] **Step 2: Run typecheck**

Run: `cd frontend && bun run build`
Expected: build succeeds.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/hooks/useWorkspaceJobs.ts
git commit -m "feat(frontend): useWorkspaceJobs polling hook (3s)"
```

---

### Task 17: Sidebar indicators (spinner + percent + green dot)

**Files:**
- Modify: `frontend/src/components/Sidebar/Sidebar.tsx`

- [ ] **Step 1: Identify the workspace id**

Read the surrounding component to find how `workspaceId` (or `activeDomainId` — confirm which) is available. The current code uses `useAppStore` to get `threads`. Add a similar line that reads the workspace id.

- [ ] **Step 2: Wire the hook into Sidebar**

In `frontend/src/components/Sidebar/Sidebar.tsx`, near the existing `useAppStore` calls:

```tsx
import { useWorkspaceJobs } from "@/hooks/useWorkspaceJobs"
import { Loader2 } from "lucide-react"

// ... inside component body:
  const workspaceId = useAppStore((s) => s.activeDomainId)  // confirm field name
  const { jobsByThreadId } = useWorkspaceJobs(workspaceId)
```

- [ ] **Step 3: Render indicators on each thread row**

Replace the existing button body at `Sidebar.tsx` around line 171:

```tsx
          {threads.map((thread) => {
            const job = jobsByThreadId[thread.id]
            const lastViewed = thread.last_viewed_at ? new Date(thread.last_viewed_at) : null
            const lastUpdated = new Date(thread.updated_at)
            const hasUnread = lastViewed === null
              ? false  // never viewed → no false positives
              : lastUpdated > lastViewed
            return (
              <button
                key={thread.id}
                onClick={() => { selectThread(thread.id); navigate(`${pathPrefix}/chat`) }}
                data-testid={`sidebar-thread-${thread.id}`}
                className={`flex w-full items-center gap-2 rounded-md px-3 py-1.5 text-left text-sm transition-colors ${
                  thread.id === threadId
                    ? "bg-accent text-accent-foreground"
                    : "text-muted-foreground hover:bg-accent hover:text-accent-foreground"
                }`}
              >
                <span className="flex-1 truncate">{thread.title}</span>
                {job ? (
                  <span
                    className="flex items-center gap-1 text-xs"
                    data-testid={`sidebar-thread-job-${thread.id}`}
                    title={job.progress?.message ?? "Materializing..."}
                  >
                    <Loader2 className="h-3 w-3 animate-spin" />
                    {job.progress?.percent != null && (
                      <span>{job.progress.percent}%</span>
                    )}
                  </span>
                ) : hasUnread ? (
                  <span
                    className="h-2 w-2 rounded-full bg-green-500"
                    data-testid={`sidebar-thread-unread-${thread.id}`}
                  />
                ) : null}
              </button>
            )
          })}
```

- [ ] **Step 4: Run lint/build**

Run: `cd frontend && bun run lint && bun run build`
Expected: clean.

- [ ] **Step 5: Manual visual check**

Start the dev server: `uv run honcho -f Procfile.dev start` (or `cd frontend && bun dev`).
Open the app in a browser, confirm: existing threads still render normally, no spinners shown when no jobs are active, no green dots on never-viewed threads.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/components/Sidebar/Sidebar.tsx
git commit -m "feat(frontend): sidebar spinner+percent + green-dot unread indicator"
```

---

### Task 18: Rewire chat tool card to use the hook

**Files:**
- Modify: `frontend/src/components/ChatMessage/ChatMessage.tsx`

- [ ] **Step 1: Read the existing ToolCallPart**

Read `frontend/src/components/ChatMessage/ChatMessage.tsx` lines 130-220. Understand:
- `AUTO_EXPAND_TOOLS` includes `run_materialization` (keep this).
- The `showCancelButton` block currently keys off `isLoading` from SSE; we need to key off the hook's `jobsByThreadId` for this thread.
- The progress text rendering reads from `part.output` SSE chunks; we need to read from `job.progress`.

- [ ] **Step 2: Make `ChatMessage` accept the current `job` for the thread**

Easiest path: thread the active `job` (or `null`) in as a prop from the parent (`ChatPanel`), which already has `workspaceId` and `threadId`. Add a prop:

```tsx
interface ToolCallPartProps {
  part: any
  index: number
  isLatest: boolean
  isActiveMessage: boolean
  workspaceId?: string
  activeMaterializationJob?: import("@/api/jobs").ActiveJob | null
}
```

Update the parent component (likely `ChatMessage` itself) to forward this prop.

- [ ] **Step 3: Rewire `showCancelButton` and progress rendering**

Replace the `showCancelButton` and the cancel POST URL:

```tsx
  const showCancelButton =
    toolName === "run_materialization"
    && !!activeMaterializationJob
    && ["pending", "running"].includes(activeMaterializationJob.state)
    && isActiveMessage
    && !!workspaceId

  const handleCancel = async (e: React.MouseEvent) => {
    e.stopPropagation()
    if (!workspaceId || !activeMaterializationJob || cancelState === "pending") return
    setCancelState("pending")
    try {
      await api.post(
        `/api/workspaces/${workspaceId}/jobs/${activeMaterializationJob.thread_job_id}/cancel/`,
        {},
      )
    } catch {
      setCancelState("error")
      setTimeout(() => setCancelState("idle"), 3000)
    }
  }
```

In the expanded tool body, when `toolName === "run_materialization"` AND `activeMaterializationJob` is non-null, render live progress from the hook instead of `part.output`:

```tsx
  {toolName === "run_materialization" && activeMaterializationJob ? (
    <div className="px-3 py-2 text-xs text-muted-foreground">
      ⏳ {activeMaterializationJob.progress?.message ?? "Materializing..."}
      {activeMaterializationJob.progress?.rows_loaded != null && (
        <> ({activeMaterializationJob.progress.rows_loaded.toLocaleString()}
        {activeMaterializationJob.progress.rows_total
          ? ` / ${activeMaterializationJob.progress.rows_total.toLocaleString()}`
          : ""})
        </>
      )}
    </div>
  ) : null}
```

- [ ] **Step 4: Forward `activeMaterializationJob` from `ChatPanel`**

In `frontend/src/components/ChatPanel/ChatPanel.tsx` (or wherever ChatMessage is rendered):

```tsx
import { useWorkspaceJobs } from "@/hooks/useWorkspaceJobs"

// ... inside the component:
const { jobsByThreadId } = useWorkspaceJobs(workspaceId)
const activeJob = jobsByThreadId[threadId] ?? null

// when rendering each message:
<ChatMessage ... activeMaterializationJob={activeJob} ... />
```

Note: the hook is intentionally re-instantiable at multiple call sites — each instance polls independently. If shared state becomes painful, lift it to a Zustand slice; for now this is fine because both call sites use the same workspace id.

- [ ] **Step 5: Run lint/build**

Run: `cd frontend && bun run lint && bun run build`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/components/ChatMessage/ChatMessage.tsx frontend/src/components/ChatPanel/ChatPanel.tsx
git commit -m "feat(frontend): wire chat tool card to useWorkspaceJobs (cancel + progress)"
```

---

### Task 19: Mark thread viewed on open

**Files:**
- Modify: the thread-open handler (likely in the Zustand store at `frontend/src/store/`)

- [ ] **Step 1: Locate `selectThread` action**

Run: `grep -rn "selectThread" frontend/src/` to find the Zustand action body.

- [ ] **Step 2: Call `markThreadViewed` inside `selectThread`**

In the store action:

```typescript
import { markThreadViewed } from "@/api/threads"

selectThread: async (threadId: string) => {
  set({ threadId })
  const workspaceId = get().activeDomainId  // adapt to actual field
  if (workspaceId) {
    try { await markThreadViewed(workspaceId, threadId) } catch { /* swallow */ }
    // Also refresh the threads list so the new last_viewed_at appears.
    await get().uiActions.fetchThreads(workspaceId)
  }
},
```

- [ ] **Step 3: Manual verification**

Start dev server, open a thread with a green dot, confirm the dot disappears.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/store/
git commit -m "feat(frontend): mark threads viewed on open (clears green dot)"
```

---

## Phase 6 — End-to-end verification

### Task 20: Playwright-cli walkthrough

**Files:**
- N/A (manual verification using `playwright-cli`)

- [ ] **Step 1: Initialize Playwright in the project directory if needed**

```bash
ls .playwright 2>/dev/null || bunx playwright-cli install chromium
```

- [ ] **Step 2: Start the full dev stack**

```bash
docker compose up -d platform-db mcp-server
uv run honcho -f Procfile.dev start &
```

Wait for Django to come up on :8000 and Vite on :5173.

- [ ] **Step 3: Drive the UI**

```bash
playwright-cli open http://localhost:5173
playwright-cli snapshot
```

Log in, create or open a workspace with a real CommCare connection, open a chat, send "Load all the data."

- [ ] **Step 4: Observe**

```bash
playwright-cli screenshot         # capture sidebar with spinner
playwright-cli console            # confirm no errors
```

Expected:
- Agent acknowledges in ~1s with one sentence ("I've started loading...").
- Sidebar shows a spinner with a percent next to the active thread.
- After ~minutes, materialization completes; a new agent response appears in the thread.
- After navigating away and back, sidebar shows green dot, then dot clears on open.

- [ ] **Step 5: Cancel mid-flight**

Trigger again, click the in-chat Stop button mid-materialization.
Expected: Spinner disappears, agent's resume response acknowledges the cancellation ("I wasn't able to load the data..."), `MaterializationRun.state = CANCELLED` in DB.

- [ ] **Step 6: Document any rough edges**

Note any UI polish required (spacing, copy, timing) and file follow-ups. Do not block the PR on cosmetic items.

- [ ] **Step 7: Final commit (if any tweaks)**

```bash
git add -p     # selective stage
git commit -m "polish(frontend): minor adjustments from e2e walkthrough"
```

---

## Final review checklist

- [ ] All migrations run cleanly (`uv run python manage.py migrate`).
- [ ] All backend tests pass (`uv run pytest`).
- [ ] Frontend builds and lints (`cd frontend && bun run lint && bun run build`).
- [ ] `grep -rn "_MATERIALIZATION_POLL\|_format_progress_message\|progress_queue\|_PROGRESS_TOOLS" .` returns nothing.
- [ ] `AGENT_TIMEOUT_SECONDS = 300` is still in `apps/chat/stream.py` (only the old progress plumbing was removed).
- [ ] One green-dot/spinner/cancel walkthrough completes in the browser.

## Out of scope (do not implement)

- Workspace-level "Materialization in progress" top banner.
- Sidebar Stop button (right-click reveal).
- Toast on completion.
- xmlns form-type filtering.
- Generic background-job UI for non-materialization tasks (the model supports it; UI doesn't change yet).
