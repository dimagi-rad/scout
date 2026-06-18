# Recipe Headless Materialization — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make recipe runs that need fresh data work end-to-end by giving the *headless* recipe execution path its own *blocking* materialization, instead of crashing on the *interactive* chat-only `run_materialization` (fire-and-resume) tool.

**Architecture:** The LangGraph agent is invoked from two front-ends — interactive **chat** (real `Thread` + Postgres checkpointer + fire-and-ack + async resume) and the **recipe runner** (one-shot, thread-less, `checkpointer=None`). The MCP `run_materialization` tool assumes the chat contract: it casts the injected `thread_id` to a `Thread` UUID, binds a `ThreadJob`, and relies on `resume_thread_after_materialization` to deliver results into the persisted thread later. A recipe's synthetic `thread_id="recipe-run-<uuid>"` is neither a UUID nor a real `Thread`, so the cast crashes — and even patched, fire-and-ack can't deliver into a one-shot recipe run. The fix makes **execution mode explicit** at the graph boundary (`interactive` vs headless): headless runs get a *blocking* materialize that reuses the existing pipeline core and returns when data is loaded, and headless recipe execution moves to a background Procrastinate task so it can block without holding an HTTP connection open.

**Tech Stack:** Django 5 (async ORM), LangGraph + langchain-anthropic, FastMCP, Procrastinate (Postgres task queue), pytest / pytest-asyncio, React 19 + Vite frontend.

## Global Constraints

- **Imports at module level**, never inside function bodies (exceptions: optional deps guarded by `try/except ImportError`; code that must run before `django.setup()`). When moving an inline import to module level, update any `mock.patch()` target to the *consuming* module.
- **Async-first ORM:** use `.aget()/.afirst()/.acreate()/.aupdate()/async for` in async code; never sync ORM from an async view/task. Sync pipeline code (`run_pipeline`) is called via `asyncio.to_thread`.
- **ruff** (line-length 100, py311 target, rules E/F/I/UP/B/ASYNC/DJ/S/SIM/TRY/RUF/PTH). Run `uv run ruff check . && uv run ruff format --check .` before each commit.
- **Tests:** async DB tests use `@pytest.mark.asyncio` + `@pytest.mark.django_db(transaction=True)`.
- **`data-testid`** on any new interactive frontend element (kebab `{component}-{element}`).
- Work in the worktree `/Users/bderenzi/Code/dimagi/scout-recipe-fix` on branch `bdr/recipe-materialization-fix` (branched from `origin/main`). Branch the PR against `main`.

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `mcp_server/server.py` | MCP `run_materialization` tool | **Modify** — guard non-UUID `thread_id` (defensive crash-stop, all callers) |
| `apps/workspaces/tasks.py` | Materialization task + reusable core | **Modify** — extract `materialize_workspace_core(...)`; task delegates + keeps resume `finally` |
| `apps/agents/tools/materialization_tool.py` | Headless blocking materialize agent-side tool | **Create** |
| `apps/agents/graph/base.py` | Graph build, prompt injection, tool gating | **Modify** — `interactive` mode param; gate prompt + tool selection |
| `apps/recipes/services/runner.py` | Recipe runner | **Modify** — headless graph; operate on a pre-created `RecipeRun`; accept `job_id` |
| `apps/recipes/tasks.py` | Background recipe execution task | **Create** |
| `apps/recipes/api/views.py` | Recipe run endpoint | **Modify** — create `RecipeRun(PENDING)`, defer task, return 202 |
| `frontend/src/pages/RecipesPage/RecipeDetail.tsx` (+ hook) | Recipe run UI | **Modify** — poll a running recipe to terminal state |

Tasks 0–4 are an independently mergeable, testable slice (crash fixed + materializing recipes work, still inline). Tasks 5–6 layer the background-task migration on top. If 5–6 prove too large for one PR, ship 0–4 first.

---

### Task 0: Defensive guard — `run_materialization` rejects a non-Thread `thread_id`

Stops the production 500-crash class for *any* caller (not just recipes): a malformed/non-UUID `thread_id` must yield a clean tool error, never a `ValueError` out of a UUIDField cast.

**Files:**
- Modify: `mcp_server/server.py` (the `run_materialization` tool, around the `Thread.objects.filter(id=thread_id,...)` check, ~`:560-570`)
- Test: `tests/test_run_materialization_fire_and_ack.py`

**Interfaces:**
- Produces: no signature change. Behavior: when `thread_id` is not a well-formed UUID, return `error_response(VALIDATION_ERROR, "thread_id must be a valid thread identifier")` instead of raising.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_run_materialization_fire_and_ack.py
@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_run_materialization_rejects_non_uuid_thread_id():
    from mcp_server.server import run_materialization

    workspace, user = await _make_workspace_with_membership()  # existing helper in this file
    result = await run_materialization(
        workspace_id=str(workspace.id),
        user_id=str(user.id),
        thread_id="recipe-run-f3be369b-d867-4ef8-aa0d-a74f21101c18",
        tool_call_id="call_1",
    )
    assert result["status"] == "error"
    assert "thread" in result["error"]["message"].lower()
    # Must NOT have raised / created a ThreadJob
    from apps.chat.models import ThreadJob
    assert not await ThreadJob.objects.aexists()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_run_materialization_fire_and_ack.py::test_run_materialization_rejects_non_uuid_thread_id -v`
Expected: FAIL — currently raises `ValueError: badly formed hexadecimal UUID string` (or `ValidationError`).

- [ ] **Step 3: Add the guard before the Thread existence check**

In `run_materialization`, immediately after the `if not thread_id:` validation and before `_resolve_workspace_memberships`, add:

```python
import uuid as _uuid  # module-level import at top of file
...
        # thread_id is injected server-side and is expected to be a real chat
        # Thread UUID. A non-UUID value (e.g. a recipe runner's synthetic
        # "recipe-run-<id>") must fail cleanly here rather than crash the
        # UUIDField cast in the Thread lookup below. Headless callers that need
        # materialization use the blocking agent-side tool, not this one.
        try:
            _uuid.UUID(str(thread_id))
        except (ValueError, AttributeError, TypeError):
            tc["result"] = error_response(
                VALIDATION_ERROR, "thread_id must be a valid thread identifier"
            )
            return tc["result"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_run_materialization_fire_and_ack.py -v`
Expected: PASS (new test + all existing run_materialization tests stay green).

- [ ] **Step 5: Commit**

```bash
git add mcp_server/server.py tests/test_run_materialization_fire_and_ack.py
git commit -m "fix(mcp): run_materialization rejects non-UUID thread_id cleanly (stops recipe crash)"
```

---

### Task 1: Extract `materialize_workspace_core` (pure refactor)

Make the workspace materialization loop callable directly (and synchronously awaitable) by a headless caller, separate from the Procrastinate task wrapper that defers the chat resume.

**Files:**
- Modify: `apps/workspaces/tasks.py` (`materialize_workspace` task, `:220-377`; `_run_pipeline_with_progress` `:487`)
- Test: `tests/test_materialize_workspace.py` (existing) — must stay green; add one direct-core test.

**Interfaces:**
- Produces: `async def materialize_workspace_core(workspace_id: str, user_id: str = "", job_id: int | None = None) -> dict` — returns `{"tenants": [...], "all_succeeded": bool, "view_schema": dict | None}`. Runs the tenant loop + view-schema rebuild + dependent-view rebuild. Does **not** defer any resume task.
- `materialize_workspace(context, workspace_id, user_id="")` task: `try: return await materialize_workspace_core(workspace_id, user_id, context.job.id) finally: await _defer_resume_for_job(context.job.id)`.
- `_run_pipeline_with_progress(..., job_id: int | None)` — widen `job_id` type to accept `None`.

- [ ] **Step 1: Write the failing test (direct core call, no resume)**

```python
# tests/test_materialize_workspace.py
@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_materialize_workspace_core_runs_without_deferring_resume(monkeypatch):
    from apps.workspaces import tasks as wtasks

    called = {"resume": False}
    async def _no_resume(job_id):
        called["resume"] = True
    monkeypatch.setattr(wtasks, "_defer_resume_for_job", _no_resume)
    # Stub the per-tenant pipeline so no real export runs.
    def _fake_pipeline(tm, cred, cfg, job_id):
        return {"sources": {}}
    monkeypatch.setattr(wtasks, "_run_pipeline_with_progress", _fake_pipeline)

    workspace, _user = await _make_single_tenant_workspace_with_credential()  # existing helper
    result = await wtasks.materialize_workspace_core(str(workspace.id), "", job_id=None)

    assert result["all_succeeded"] is True
    assert called["resume"] is False  # core must NOT defer resume
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_materialize_workspace.py::test_materialize_workspace_core_runs_without_deferring_resume -v`
Expected: FAIL — `AttributeError: module 'apps.workspaces.tasks' has no attribute 'materialize_workspace_core'`.

- [ ] **Step 3: Extract the core**

Move the entire body of `materialize_workspace` between `job_id = context.job.id` and the `finally:` (i.e. the `try:` block contents, `:239-372`) into a new module-level async function. Replace the task body with a thin wrapper:

```python
async def materialize_workspace_core(
    workspace_id: str, user_id: str = "", job_id: int | None = None
) -> dict:
    """Run materialization for all tenants in a workspace and rebuild view
    schemas. Returns a per-tenant summary. Does NOT defer any chat-resume task
    — callers that need the interactive resume use the materialize_workspace
    Procrastinate task; headless callers (recipes) call this directly and block.
    """
    tenant_results: list[dict] = []
    # ... (verbatim moved body: load workspace, memberships, per-tenant loop
    #      calling _run_pipeline_with_progress(tm, credential, pipeline_config, job_id),
    #      view-schema rebuild, _rebuild_dependent_view_schemas, return {...}) ...


@task(pass_context=True)
async def materialize_workspace(context, workspace_id: str, user_id: str = "") -> dict:
    job_id = context.job.id
    try:
        return await materialize_workspace_core(workspace_id, user_id, job_id)
    finally:
        await _defer_resume_for_job(job_id)
```

Widen `_run_pipeline_with_progress(..., job_id: int | None)`. (The `updater` closure keys off `progress["run_id"]`, not `job_id`, so `None` is safe; `run_pipeline` already declares `procrastinate_job_id: int | None = None`.)

- [ ] **Step 4: Run tests to verify all green**

Run: `uv run pytest tests/test_materialize_workspace.py -v`
Expected: PASS (existing task tests + new core test).

- [ ] **Step 5: Commit**

```bash
git add apps/workspaces/tasks.py tests/test_materialize_workspace.py
git commit -m "refactor(tasks): extract materialize_workspace_core for reuse by headless callers"
```

---

### Task 2: Headless blocking-materialize agent-side tool

A LangChain tool the headless agent calls instead of the MCP fire-and-ack tool. It runs in the same process as the recipe task, blocks on `materialize_workspace_core`, and returns a completion summary. It gets `workspace`/`user`/`job_id` by closure (no `thread_id` injection, so no UUID landmine).

**Files:**
- Create: `apps/agents/tools/materialization_tool.py`
- Test: `tests/test_materialization_tool.py` (create)

**Interfaces:**
- Consumes: `apps.workspaces.tasks.materialize_workspace_core`.
- Produces: `def create_materialization_tool(workspace: Workspace, user: User | None, job_id: int | None = None) -> BaseTool` — returns a tool **named `run_materialization`** (so the agent's behavior/prompt are mode-agnostic), zero LLM-facing params, async. On call: `summary = await materialize_workspace_core(str(workspace.id), str(user.id) if user else "", job_id)`; returns a dict `{"status": "completed"|"partial"|"failed", "tenants_loaded": N, "message": "..."}`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_materialization_tool.py
@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_headless_materialization_tool_blocks_and_reports_completion(monkeypatch):
    from apps.agents.tools import materialization_tool as mt

    async def _fake_core(workspace_id, user_id="", job_id=None):
        return {"all_succeeded": True, "tenants": [{"tenant": "t1", "success": True}],
                "view_schema": None}
    monkeypatch.setattr(mt, "materialize_workspace_core", _fake_core)

    workspace, user = await _make_single_tenant_workspace_with_credential()
    tool = mt.create_materialization_tool(workspace, user, job_id=99)
    assert tool.name == "run_materialization"
    result = await tool.ainvoke({})
    assert result["status"] == "completed"
    assert result["tenants_loaded"] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_materialization_tool.py -v`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement the tool**

```python
# apps/agents/tools/materialization_tool.py
"""Headless blocking materialization tool for non-interactive agent runs (recipes)."""
from __future__ import annotations

from typing import TYPE_CHECKING

from langchain_core.tools import StructuredTool

from apps.workspaces.tasks import materialize_workspace_core

if TYPE_CHECKING:
    from apps.users.models import User
    from apps.workspaces.models import Workspace


def create_materialization_tool(workspace: "Workspace", user: "User | None", job_id: int | None = None):
    """Build a `run_materialization` tool that loads data and BLOCKS until done.

    Unlike the interactive MCP tool (fire-and-ack + async resume), this runs the
    pipeline inline and returns a completion summary, suitable for one-shot
    headless runs that have no chat thread/checkpointer to resume into.
    """
    workspace_id = str(workspace.id)
    user_id = str(user.id) if user else ""

    async def _run() -> dict:
        summary = await materialize_workspace_core(workspace_id, user_id, job_id)
        tenants = summary.get("tenants", [])
        loaded = sum(1 for t in tenants if t.get("success"))
        vs = summary.get("view_schema")
        if summary.get("all_succeeded") and (vs is None or vs.get("ok")):
            status, message = "completed", "Data loaded successfully. Continue with the analysis."
        elif loaded:
            status, message = "partial", "Some tenants loaded; others failed. Proceed with available data."
        else:
            status, message = "failed", "Materialization failed; no data was loaded."
        return {"status": status, "tenants_loaded": loaded, "message": message}

    return StructuredTool.from_function(
        coroutine=_run,
        name="run_materialization",
        description=(
            "Load/refresh this workspace's data from source. Blocks until loading "
            "completes, then returns a status summary. Call this before querying "
            "when data is not yet loaded."
        ),
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_materialization_tool.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/agents/tools/materialization_tool.py tests/test_materialization_tool.py
git commit -m "feat(agents): headless blocking run_materialization tool"
```

---

### Task 3: Explicit `interactive` mode in `build_agent_graph` (the systemic guard)

Make the Thread/checkpointer contract visible at the call boundary. `interactive=True` (default) keeps today's chat behavior. `interactive=False` swaps the materialization tool (MCP fire-and-ack → headless blocking) and the "no data" system-prompt nudge (resume → blocking).

**Files:**
- Modify: `apps/agents/graph/base.py` — `build_agent_graph` signature `:498`; `_build_tools` `:690`; `AGENT_EXCLUDED_MCP_TOOLS` usage; the schema-status prompt fn `:225-255`.
- Test: `tests/test_agent_graph.py` (existing).

**Interfaces:**
- Consumes: `apps.agents.tools.materialization_tool.create_materialization_tool`.
- Produces: `build_agent_graph(workspace, user=None, checkpointer=None, mcp_tools=None, oauth_tokens=None, conversation_id=None, *, interactive: bool = True, job_id: int | None = None)`. When `interactive=False`: the MCP `run_materialization` tool is excluded and the headless `create_materialization_tool(workspace, user, job_id)` is appended; the "no data loaded" prompt tells the agent the tool **blocks and returns when done** (no "end your turn / will be resumed").
- The schema-status prompt builder takes an `interactive: bool` arg and returns the blocking variant when `False`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_agent_graph.py
@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_headless_graph_swaps_in_blocking_materialization_tool():
    workspace, user = await _make_single_tenant_workspace_with_credential()
    mcp_tools = _fake_mcp_tools()  # includes an MCP "run_materialization"
    graph_tools = await _tools_for(build_agent_graph, workspace, user, mcp_tools, interactive=False)
    names = [t.name for t in graph_tools]
    assert names.count("run_materialization") == 1  # exactly one, not duplicated
    rm = next(t for t in graph_tools if t.name == "run_materialization")
    assert rm.coroutine is not None  # the headless StructuredTool, not the MCP tool

def test_no_data_prompt_blocking_variant_for_headless():
    from apps.agents.graph.base import _schema_status_message  # name per existing code
    msg = _schema_status_message(loaded=False, materializing=False, interactive=False)
    assert "end your turn" not in msg.lower()
    assert "block" in msg.lower() or "returns when" in msg.lower()
```

(Adjust helper/function names to the file's actual internals; the prompt text currently lives in the schema-status function near `:225-255` — give it an `interactive` parameter.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_agent_graph.py -k "headless or blocking" -v`
Expected: FAIL — `interactive` kwarg unsupported / prompt has no variant.

- [ ] **Step 3: Implement mode gating**

1. Add `*, interactive: bool = True, job_id: int | None = None` to `build_agent_graph`.
2. Pass `interactive`/`job_id` into `_build_tools(workspace, user, mcp_tools, conversation_id=..., interactive=interactive, job_id=job_id)`.
3. In `_build_tools`: when `not interactive`, filter the MCP tool list to drop `run_materialization` (extend the existing `AGENT_EXCLUDED_MCP_TOOLS` filtering with a per-call exclusion set), then append `create_materialization_tool(workspace, user, job_id)`.
4. Give the schema-status prompt function an `interactive` parameter; when `False`, return:
   `"No data has been loaded yet. Call `run_materialization` to load it — this tool BLOCKS and returns a status summary when loading finishes. After it returns `completed`, continue with the requested analysis in the same run."` (and a matching variant for the `MATERIALIZING` branch). Thread `interactive` from `build_agent_graph` to wherever this message is assembled (system-prompt assembly).

- [ ] **Step 4: Run tests to verify green**

Run: `uv run pytest tests/test_agent_graph.py -v`
Expected: PASS (new + existing; interactive default path unchanged).

- [ ] **Step 5: Commit**

```bash
git add apps/agents/graph/base.py tests/test_agent_graph.py
git commit -m "feat(agents): explicit interactive/headless graph mode gating materialization tool + prompt"
```

---

### Task 4: Recipe runner uses headless mode

Point the runner at `interactive=False`, pass its `job_id`, and stop relying on the synthetic `thread_id` for anything thread-coupled. `RecipeRunner` operates on a pre-created `RecipeRun` (prep for Task 5) and takes an optional `job_id`.

**Files:**
- Modify: `apps/recipes/services/runner.py` (`__init__`, `_build_graph` `:90-121`, `execute_async` `:153-224`)
- Test: `tests/test_recipes.py` (existing runner tests)

**Interfaces:**
- Produces: `RecipeRunner(recipe, variable_values, user, graph=None, run=None, job_id=None)`. `_build_graph` calls `build_agent_graph(workspace=..., user=..., checkpointer=None, mcp_tools=..., oauth_tokens=..., conversation_id=self._thread_id, interactive=False, job_id=self._job_id)`. If `run` is provided, `execute_async` uses it instead of `acreate`-ing a new one.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_recipes.py
@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_runner_builds_headless_graph(monkeypatch):
    captured = {}
    async def _fake_build(**kwargs):
        captured.update(kwargs)
        return _stub_graph_that_returns_artifact()  # existing/added stub
    monkeypatch.setattr("apps.recipes.services.runner.build_agent_graph", _fake_build)

    recipe, user = await _make_recipe_with_workspace()
    runner = RecipeRunner(recipe=recipe, variable_values={}, user=user, job_id=123)
    await runner.execute_async()
    assert captured["interactive"] is False
    assert captured["job_id"] == 123
    assert captured["checkpointer"] is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_recipes.py::test_runner_builds_headless_graph -v`
Expected: FAIL — `__init__` has no `job_id`; build called without `interactive`.

- [ ] **Step 3: Implement**

- Add `run=None, job_id=None` to `__init__`; store `self._job_id = job_id`, `self._run = run`.
- In `execute_async`: if `self._run is None`, `acreate` as today (back-compat for direct callers/tests); else use the provided run and set it `RUNNING`.
- In `_build_graph`: add `interactive=False, job_id=self._job_id` to the `build_agent_graph(...)` call.
- Keep `self._thread_id = f"recipe-run-{self._run.id}"` and `conversation_id=self._thread_id` (artifact provenance). It is no longer injected into any UUID-casting tool (headless `run_materialization` ignores it; other MCP tools never read it).

- [ ] **Step 4: Run tests to verify green**

Run: `uv run pytest tests/test_recipes.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/recipes/services/runner.py tests/test_recipes.py
git commit -m "feat(recipes): runner builds headless agent graph (blocking materialize, no thread contract)"
```

---

### Task 5: Move recipe execution to a background Procrastinate task

Recipe runs can now legitimately block on materialization, so they must not run inline in the HTTP request. The view creates a `RecipeRun(PENDING)`, defers `run_recipe`, and returns 202; the task runs the agent and finalizes the run; the frontend polls.

**Files:**
- Create: `apps/recipes/tasks.py`
- Modify: `apps/recipes/api/views.py` (`recipe_run_view` `:95-145`)
- Test: `tests/test_recipes.py`, `tests/test_recipe_run_view.py` (create if absent)

**Interfaces:**
- Consumes: `RecipeRunner(recipe, variable_values, user, run=..., job_id=...)`.
- Produces: Procrastinate task `run_recipe(context, recipe_run_id: str)` (queue `"recipes"`). It loads the `RecipeRun` + recipe + user, sets `RUNNING`, runs `RecipeRunner(..., run=run, job_id=context.job.id).execute_async()`. The view returns HTTP 202 with the `RecipeRun` (status `pending`).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_recipe_run_view.py
@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_run_recipe_view_defers_task_and_returns_202(monkeypatch):
    deferred = {}
    async def _fake_defer(recipe_run_id):
        deferred["id"] = recipe_run_id
    monkeypatch.setattr("apps.recipes.api.views.run_recipe.defer_async", _fake_defer)

    recipe, user = await _make_recipe_with_workspace()
    client = AsyncClient(); await sync_to_async(client.login)(email=user.email, password="pass")
    resp = await client.post(f"/api/workspaces/{recipe.workspace_id}/recipes/{recipe.id}/run/",
                             data={}, content_type="application/json")
    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "pending"
    assert deferred["id"] == body["id"]
    from apps.recipes.models import RecipeRun
    assert await RecipeRun.objects.filter(id=body["id"], status="pending").aexists()
```

```python
# tests/test_recipes.py
@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_run_recipe_task_executes_and_finalizes(monkeypatch):
    from apps.recipes import tasks as rtasks
    recipe, user = await _make_recipe_with_workspace()
    run = await RecipeRun.objects.acreate(recipe=recipe, run_by=user, status="pending",
                                          variable_values={}, step_results=[])
    async def _fake_exec(self):
        self._run.status = "completed"; await self._run.asave(update_fields=["status"]); return self._run
    monkeypatch.setattr("apps.recipes.services.runner.RecipeRunner.execute_async", _fake_exec)

    ctx = types.SimpleNamespace(job=types.SimpleNamespace(id=7))
    await rtasks.run_recipe(ctx, str(run.id))
    await run.arefresh_from_db()
    assert run.status == "completed"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_recipe_run_view.py tests/test_recipes.py::test_run_recipe_task_executes_and_finalizes -v`
Expected: FAIL — `run_recipe` undefined; view returns 201 sync.

- [ ] **Step 3: Implement the task**

```python
# apps/recipes/tasks.py
"""Background execution of recipe runs."""
import logging

from apps.procrastinate import app  # use the project's procrastinate App (confirm import path)
from apps.recipes.models import Recipe, RecipeRun, RecipeRunStatus
from apps.recipes.services.runner import RecipeRunner
from apps.users.models import User

logger = logging.getLogger(__name__)


@app.task(pass_context=True, queue="recipes")
async def run_recipe(context, recipe_run_id: str) -> dict:
    try:
        run = await RecipeRun.objects.select_related("recipe", "run_by").aget(id=recipe_run_id)
    except RecipeRun.DoesNotExist:
        logger.warning("run_recipe: RecipeRun %s not found", recipe_run_id)
        return {"status": "missing"}
    runner = RecipeRunner(
        recipe=run.recipe, variable_values=run.variable_values, user=run.run_by,
        run=run, job_id=context.job.id,
    )
    await runner.execute_async()
    return {"status": run.status}
```

(Confirm the canonical Procrastinate `App` import — grep `procrastinate` in `config/` / `apps/`; reuse the same `app`/`@task` that `materialize_workspace` uses. Register the `recipes` queue with the worker config in `config/deploy-worker.yml`; note a dedicated worker/queue avoids long recipe runs starving chat materializations.)

- [ ] **Step 4: Implement the view change**

In `recipe_run_view`, after validation: create the run, defer, return 202.

```python
    run = await RecipeRun.objects.acreate(
        recipe=recipe, run_by=user, status=RecipeRunStatus.PENDING,
        variable_values=variable_values, step_results=[],
    )
    await run_recipe.defer_async(recipe_run_id=str(run.id))
    return JsonResponse(RecipeRunSerializer(run).data, status=202)
```

Keep the variable-validation step before creating the run (move `RecipeRunner.validate_variables` out so it can run pre-dispatch, OR validate in the serializer; raise 400 on `VariableValidationError`). Drop the inline `runner.execute()` path entirely.

- [ ] **Step 5: Run tests to verify green**

Run: `uv run pytest tests/test_recipe_run_view.py tests/test_recipes.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add apps/recipes/tasks.py apps/recipes/api/views.py config/deploy-worker.yml tests/
git commit -m "feat(recipes): execute recipe runs in a background task (async, can block on materialize)"
```

---

### Task 6: Frontend — poll a running recipe to terminal state

The run endpoint now returns a `pending` run instead of a finished one. The recipe detail UI must poll until `completed`/`failed`, then render results/artifacts.

**Files:**
- Modify: `frontend/src/pages/RecipesPage/RecipeDetail.tsx` and its data hook (e.g. `frontend/src/hooks/useRecipes.ts` — confirm exact hook).
- Test: frontend lint (`cd frontend && bun run lint`); existing Vitest if recipe hook tests exist.

**Interfaces:**
- Consumes: `POST .../run/` → 202 `{id, status:"pending"}`; `GET .../runs/<id>/` → `{status, step_results, ...}`.

- [ ] **Step 1: Implement polling**

On run start, store the returned run id and poll `GET /api/workspaces/${workspaceId}/recipes/${recipeId}/runs/${runId}/` every ~2s while `status in ("pending","running")`; stop on terminal status and surface `step_results`/artifacts. Reuse the existing job-polling pattern (`useWorkspaceJobs.ts`) for cadence/backoff. Add `data-testid="recipe-run-status"` to the status indicator and keep the existing `recipe-run-${run.id}` / `recipe-run-view-${run.id}` testids.

- [ ] **Step 2: Verify build + lint**

Run: `cd frontend && bun run lint && bun run build`
Expected: clean.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/pages/RecipesPage/RecipeDetail.tsx frontend/src/hooks/
git commit -m "feat(frontend): poll async recipe runs to completion"
```

---

### Task 7: End-to-end integration test (recipe materializes → builds artifact, headless, no crash)

**Files:**
- Test: `tests/test_recipe_materialization_integration.py` (create)

- [ ] **Step 1: Write the test**

Drive `RecipeRunner.execute_async` with a real headless graph but a stubbed pipeline (`monkeypatch` `materialize_workspace_core` to return `all_succeeded=True`) and a stubbed LLM that emits a `run_materialization` tool call then a `create_artifact` tool call. Assert: no exception; `RecipeRun.status == completed`; `step_results[0]["artifacts_created"]` non-empty; an `Artifact` row exists for the workspace; and `run_materialization` was the blocking tool (no `Thread`/`ThreadJob` created).

- [ ] **Step 2: Run + verify green**

Run: `uv run pytest tests/test_recipe_materialization_integration.py -v`
Expected: PASS.

- [ ] **Step 3: Full suite + lint, then commit**

```bash
uv run pytest -q && uv run ruff check . && uv run ruff format --check .
git add tests/test_recipe_materialization_integration.py
git commit -m "test(recipes): e2e headless materialize+artifact recipe run"
```

---

## Self-Review notes

- **Crash fixed:** Task 0 (defensive) + Task 4 (recipes no longer use the MCP tool) — both, defense-in-depth.
- **Materializing recipes work:** Tasks 1–4 (blocking core + headless tool + mode + runner) + Task 7 (e2e).
- **Systemic root closed:** Task 3 makes the Thread/checkpointer contract explicit at the boundary; any future headless front-end sets `interactive=False` and gets safe behavior by construction.
- **Robustness:** Task 5 moves blocking execution off the request path; Task 6 keeps the UI honest while it runs.
- **Type consistency:** `materialize_workspace_core(workspace_id, user_id, job_id)` is referenced identically in Tasks 1/2/3/runner; tool name `run_materialization` is consistent across MCP (interactive) and headless. `RecipeRunner(..., run=, job_id=)` is consistent across Tasks 4/5.
- **Open confirmations for the implementer (verify against code, do not assume):** exact Procrastinate `App`/`@task` import path (reuse `materialize_workspace`'s); the schema-status prompt function's real name/params near `base.py:225`; the frontend recipe hook filename; existing test helper names (`_make_*`). None change the design.
