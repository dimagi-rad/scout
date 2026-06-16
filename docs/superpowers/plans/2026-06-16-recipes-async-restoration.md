# Recipe Async-First Restoration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore the 100%-broken recipes feature (arch #238) with a minimal async-first fix to the runner and run view, plus a real unmocked test that would have caught the break.

**Architecture:** Make `RecipeRunner.execute_async` the single live path: fix the `build_agent_graph(...)` call to the real signature (`workspace=`, load + pass `mcp_tools`/`oauth_tokens`), fix `initial_state` to the real `AgentState` keys, and delete the sync `execute()`/`async_to_sync` path. Convert `RecipeRunView` (DRF `APIView`) to a raw `async def` Django view mirroring `chat_view`.

**Tech Stack:** Django 5 async ORM, LangGraph, `langchain_mcp_adapters`, the in-process MCP SDK transport (`mcp.shared.memory`), pytest-asyncio.

---

## Context the implementer needs (read once)

- **The drifts** (all in `apps/recipes/services/runner.py` + the run view):
  1. `_build_graph` calls `build_agent_graph(tenant_membership=…, user=…, checkpointer=None)`. Real signature (`apps/agents/graph/base.py:480`): `build_agent_graph(workspace, user=None, checkpointer=None, mcp_tools=None, oauth_tokens=None)`. → `TypeError`.
  2. `execute_async`'s `initial_state` uses `tenant_id`/`tenant_name`/`tenant_membership_id`. `AgentState` (`apps/agents/graph/state.py:80`) requires `workspace_id`, `user_id`, `user_role`, `thread_id`. Wrong keys → MCP injecting node reads `workspace_id=""` → tools fail with `VALIDATION_ERROR`.
  3. `mcp_tools` never loaded/passed.
  4. The view calls sync `execute()` (`async_to_sync` + sync `graph.invoke()`).
- **The correct pattern** is `apps/chat/views.py:153-197` (load `get_mcp_tools()` + `get_user_oauth_tokens()`, call `build_agent_graph(workspace=…, mcp_tools=…, oauth_tokens=…)`, config `{"configurable": {"thread_id":…}, "recursion_limit": 50, "oauth_tokens":…}`).
- **Async-first is non-negotiable.** No `async_to_sync`. Do NOT move execution to a background task (#267). Keep `execute_async` (supersedes #266's "delete it"). Do not touch PR #277 / issues #276 / #266.
- **Fixtures** (`tests/conftest.py`): `user` (test@example.com / testpass123), `workspace` (1 tenant "test-domain", **no** TenantSchema, `user` is MANAGE member), `other_user`. The `recipe`/`recipe_step_1` fixtures are **module-local to `tests/test_recipes.py`** (the codebase pattern: each recipe test module defines its own `recipe` — see also `test_recipe_soft_delete.py`). `tests/test_recipe_runner.py` therefore defines its own local `recipe` fixture (depending on the conftest `user`/`workspace`).
- **`get_schema_status` contract** (`mcp_server/server.py:652`): empty `workspace_id` → `success=False, error.code="VALIDATION_ERROR"`; a workspace with 1 tenant and no ACTIVE schema → `success=True, data.state="not_provisioned"` with **no managed-DB connection**. This is the signal the contract test keys on.
- **`RecipeRunStatus`** (`apps/recipes/models.py:281`): `PENDING/RUNNING/COMPLETED/FAILED`.
- **Real-MCP-wire pattern** (modeled on commit `2320d52`, `tests/test_chat_mcp_contract.py`, not on main): `create_connected_server_and_client_session(scout_mcp)` + `load_mcp_tools(session)`.
- **Fake-LLM pattern** (from `tests/test_dangling_tool_calls.py:148-154`): `mock_llm.bind_tools.return_value = mock_bound; mock_bound.ainvoke = AsyncMock(side_effect=fake)`, patched via `patch("apps.agents.graph.base.ChatAnthropic", return_value=mock_llm)`.

## File structure

- **Modify** `apps/recipes/services/runner.py` — fix `_build_graph` + `execute_async`; delete `execute()`, `_create_run_record()`, the `async_to_sync` import, and `tenant_membership` plumbing.
- **Modify** `apps/recipes/api/views.py` — replace `RecipeRunView` (APIView) with `async def recipe_run_view`.
- **Modify** `apps/recipes/urls.py` — point `run/` at `recipe_run_view`.
- **Modify** `tests/test_recipe_runner.py` — replace the skeleton with the real unmocked contract test.
- **Modify** `tests/test_recipes.py` — migrate the 6 mocked `TestRecipeRunner` tests to `execute_async`; add the async view test.

---

## Task 1: Real unmocked graph-build contract test (the guardrail)

This is the test that would have caught #238. It builds the REAL graph through `execute_async` (no mock on `build_agent_graph`), with real MCP tools over the in-process wire and a mocked LLM that drives one `get_schema_status` tool call.

**Files:**
- Test: `tests/test_recipe_runner.py` (replace entire file)

- [ ] **Step 1: Replace `tests/test_recipe_runner.py` with the contract test**

```python
"""Real (unmocked) recipe runner <-> graph-build contract test (arch #238).

Exercises the REAL ``build_agent_graph`` through ``RecipeRunner.execute_async`` with
real MCP tools loaded over the in-process MCP SDK transport. Only the LLM and the
managed-data boundary are avoided: the build signature, the AgentState contract, and
the workspace_id injection path all run for real. Every other recipe test mocks
``build_agent_graph``, which is exactly why the March break (e26cd75) hid for months.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langchain_mcp_adapters.tools import load_mcp_tools
from mcp.shared.memory import create_connected_server_and_client_session

from apps.recipes.models import RecipeRunStatus
from apps.recipes.services.runner import RecipeRunner
from mcp_server.server import mcp as scout_mcp


def _fake_llm_driving_get_schema_status():
    """A fake ChatAnthropic whose bound model:

    1. first emits a get_schema_status tool call (workspace_id omitted, as the LLM
       would — the param is hidden from its schema and injected from state);
    2. then, once it sees the ToolMessage, echoes the tool envelope as its final
       answer so the runner captures it as the response.
    """

    async def fake_ainvoke(messages, *args, **kwargs):
        tool_msgs = [m for m in messages if isinstance(m, ToolMessage)]
        if tool_msgs:
            return AIMessage(content=str(tool_msgs[-1].content), id="ai-final")
        return AIMessage(
            content="",
            tool_calls=[{"name": "get_schema_status", "args": {}, "id": "call_1"}],
            id="ai-1",
        )

    mock_bound = MagicMock()
    mock_bound.ainvoke = AsyncMock(side_effect=fake_ainvoke)
    mock_llm = MagicMock()
    mock_llm.bind_tools.return_value = mock_bound
    return mock_llm


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_execute_async_builds_real_graph_and_flows_workspace_id(recipe, user):
    """RecipeRunner.execute_async builds the REAL agent graph and runs a real MCP
    tool call end-to-end.

    Catches every #238 drift at once:
    - build_agent_graph is called with the real signature (drift #1) — a TypeError
      here means the runner regressed;
    - mcp_tools are loaded and attached, so get_schema_status exists (drift #3);
    - workspace_id flows from initial_state through the real injecting node into the
      real MCP server (drift #2) — proven by a not_provisioned success envelope rather
      than a VALIDATION_ERROR (which is what an empty workspace_id returns).
    """
    values = {"region": "North", "limit": 10, "start_date": "2024-01-01"}

    async with create_connected_server_and_client_session(scout_mcp) as session:
        tools = await load_mcp_tools(session)
        with (
            patch(
                "apps.recipes.services.runner.get_mcp_tools",
                new=AsyncMock(return_value=tools),
            ),
            patch(
                "apps.agents.graph.base.ChatAnthropic",
                return_value=_fake_llm_driving_get_schema_status(),
            ),
        ):
            run = await RecipeRunner(recipe=recipe, variable_values=values, user=user).execute_async()

    assert run.status == RecipeRunStatus.COMPLETED, run.step_results
    step = run.step_results[0]
    assert step["success"] is True
    assert "get_schema_status" in step["tools_used"]
    # Positive proof workspace_id reached the server: a real not_provisioned envelope,
    # never the VALIDATION_ERROR that an empty workspace_id would have produced.
    assert "not_provisioned" in step["response"]
    assert "VALIDATION_ERROR" not in step["response"]
```

- [ ] **Step 2: Run it to verify it FAILS against the current (broken) runner**

Run: `uv run pytest tests/test_recipe_runner.py -v`
Expected: RED (the feature isn't implemented yet). The current runner does **not** import `get_mcp_tools` (drift #3 — mcp_tools are never loaded), so `patch("apps.recipes.services.runner.get_mcp_tools", …)` raises `AttributeError` on context entry. That is a legitimate TDD red: it directly reflects the missing-mcp_tools drift. (Once Task 2 adds `from apps.agents.mcp_client import get_mcp_tools` to the runner and fixes the `build_agent_graph` signature, the patch target exists and the assertions run for real.) Acceptable RED signatures: the `AttributeError` on the missing `get_mcp_tools`, OR — if the import were present but the signature still wrong — a FAILED run with a `TypeError` printed by the `assert run.status == RecipeRunStatus.COMPLETED` message. The test must COLLECT and RUN cleanly (no import error, no "fixture not found"); it must simply not PASS.

- [ ] **Step 3: Commit the failing test**

```bash
git add tests/test_recipe_runner.py
git commit -m "test: real unmocked recipe graph-build contract (arch #238)

Builds the real agent graph through execute_async with real MCP tools and
a mocked LLM driving a get_schema_status call. Fails on the current runner
(build_agent_graph signature drift). Will pass once the runner is fixed.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Fix the runner's async path + delete sync path + migrate unit tests (MERGED with Task 3)

> **Execution note (revised during implementation):** Tasks 2 and 3 are executed as ONE task. Reason: `_build_graph` is shared by both `execute()` and `execute_async`. Rewriting it to call `get_mcp_tools()` (unmocked in the old `execute()`-based tests) plus removing `self._tenant_membership` would break the old tests in the gap between commits. So the runner fix, the deletion of `execute()`/`_create_run_record()`, and the migration of the 6 mocked unit tests all land together. The contract test goes GREEN and the migrated unit tests (which pass `graph=mock_graph`, bypassing `_build_graph`) stay GREEN. Step 6 below ("old mocked tests still pass, execute() untouched") is superseded by Task 3's migration steps.

**Files:**
- Modify: `apps/recipes/services/runner.py`

- [ ] **Step 1: Fix the imports**

Replace the import block (lines ~15-22) so it reads:

```python
from django.utils import timezone
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from apps.agents.graph.base import build_agent_graph
from apps.agents.mcp_client import get_mcp_tools, get_user_oauth_tokens
from apps.recipes.models import Recipe, RecipeRun, RecipeRunStatus
from apps.workspaces.models import Workspace
```

(Removes `from asgiref.sync import async_to_sync`, `from apps.users.models import TenantMembership`, and `from apps.workspaces.models import WorkspaceTenant`. Keeps `json`, `logging`, `uuid`, `TYPE_CHECKING`/`Any`, and the `TYPE_CHECKING` `User`/`CompiledStateGraph` block.)

- [ ] **Step 2: Initialize `_oauth_tokens` and drop `_tenant_membership` in `__init__`**

In `__init__`, replace `self._tenant_membership = None` with:

```python
        self._oauth_tokens: dict = {}
```

- [ ] **Step 3: Rewrite `_build_graph`**

Replace the whole `_build_graph` method body with:

```python
    async def _build_graph(self) -> CompiledStateGraph:
        """Build or return the agent graph for execution (async-first)."""
        if self._provided_graph is not None:
            return self._provided_graph

        if self._graph is None:
            # Load the Workspace by FK id rather than traversing
            # ``self.recipe.workspace`` lazily, which would raise
            # SynchronousOnlyOperation under async (root of Sentry #276).
            workspace = await Workspace.objects.aget(id=self.recipe.workspace_id)
            mcp_tools = await get_mcp_tools()
            self._oauth_tokens = await get_user_oauth_tokens(self.user)
            self._graph = await build_agent_graph(
                workspace=workspace,
                user=self.user,
                checkpointer=None,
                mcp_tools=mcp_tools,
                oauth_tokens=self._oauth_tokens,
            )

        return self._graph
```

- [ ] **Step 4: Fix `execute_async`'s state, config, and remove the tenant fetch**

In `execute_async`, replace the `config` line and the whole `try:` block's tenant-fetch + `initial_state` so the section reads:

```python
        graph = await self._build_graph()
        config = {
            "configurable": {"thread_id": self._thread_id},
            "recursion_limit": 50,
            "oauth_tokens": self._oauth_tokens,
        }

        prompt = self.recipe.render_prompt(self.variable_values)

        logger.info("Starting async recipe execution: %s", self.recipe.name)

        step_started = timezone.now()

        result = {
            "step_order": 1,
            "prompt": prompt,
            "response": "",
            "tools_used": [],
            "artifacts_created": [],
            "success": False,
            "error": None,
            "started_at": step_started.isoformat(),
            "completed_at": None,
        }

        try:
            initial_state = {
                "messages": [HumanMessage(content=prompt)],
                "workspace_id": str(self.recipe.workspace_id),
                "user_id": str(self.user.id),
                "user_role": "analyst",
                "thread_id": self._thread_id,
            }

            response = await graph.ainvoke(initial_state, config=config)

            messages = response.get("messages", [])
            result["response"] = self._extract_response_content(messages)
            result["tools_used"] = self._extract_tools_used(messages)
            result["artifacts_created"] = self._extract_artifacts_created(messages)
            result["success"] = True

        except Exception as e:
            logger.exception("Error executing recipe %s (async)", self.recipe.name)
            result["error"] = str(e)
            result["success"] = False
```

(The previous `wt = await WorkspaceTenant…` fetch and the `tenant_id`/`tenant_name`/`tenant_membership_id` keys are removed.)

- [ ] **Step 5: Run the contract test — expect PASS**

Run: `uv run pytest tests/test_recipe_runner.py -v`
Expected: PASS.

- [ ] **Step 6: Confirm the old mocked tests still pass (execute() untouched)**

Run: `uv run pytest tests/test_recipes.py -q`
Expected: PASS (the sync `execute()` path is unchanged this commit).

- [ ] **Step 7: Commit**

```bash
git add apps/recipes/services/runner.py
git commit -m "fix: restore recipe execute_async graph-build contract (arch #238)

Fix _build_graph to call build_agent_graph(workspace=, mcp_tools=,
oauth_tokens=) with the real signature, load MCP tools + oauth tokens, and
fix execute_async's initial_state to the real AgentState keys
(workspace_id/user_id/user_role/thread_id). Load workspace by FK id to
avoid the SynchronousOnlyOperation (Sentry #276).

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Delete the sync path; migrate the mocked unit tests to `execute_async` (MERGED INTO TASK 2 — executed together)

**Files:**
- Modify: `apps/recipes/services/runner.py` (delete `execute()` + `_create_run_record()`)
- Modify: `tests/test_recipes.py` (6 tests in `TestRecipeRunner`)

- [ ] **Step 1: Delete `_create_run_record()` and `execute()`**

In `apps/recipes/services/runner.py`, delete the entire `_create_run_record` method and the entire sync `execute` method (everything from `def _create_run_record(self)` through the end of `def execute`, i.e. up to but not including `async def execute_async`). `execute_async` calls `RecipeRun.objects.acreate(...)` directly, so `_create_run_record` is now dead.

- [ ] **Step 2: Migrate the `TestRecipeRunner` class in `tests/test_recipes.py`**

Replace the entire `TestRecipeRunner` class (the block starting at `class TestRecipeRunner:` near line 581, through `test_recipe_runner_updates_run_status`) with the async versions below. Each test now provides a mock graph via the `graph=` constructor arg (which `_build_graph` returns immediately, bypassing the real build) and uses `AsyncMock` for `ainvoke`:

```python
@pytest.mark.django_db(transaction=True)
class TestRecipeRunner:
    """Tests for the RecipeRunner async path with a provided (mocked) agent graph."""

    @pytest.mark.asyncio
    async def test_recipe_runner_validates_variables(self, recipe, user, recipe_step_1):
        """RecipeRunner.execute_async raises VariableValidationError on missing vars."""
        from apps.recipes.services.runner import RecipeRunner, VariableValidationError

        invalid_values = {"region": "North", "limit": 10}  # start_date missing

        runner = RecipeRunner(recipe, invalid_values, user, graph=Mock())
        with pytest.raises(VariableValidationError):
            await runner.execute_async()

    @pytest.mark.asyncio
    async def test_recipe_runner_creates_run_record(self, recipe, user, recipe_step_1):
        """RecipeRunner creates a RecipeRun record."""
        from apps.recipes.services.runner import RecipeRunner

        values = {"region": "North", "limit": 10, "start_date": "2024-01-01"}
        mock_graph = Mock()
        mock_graph.ainvoke = AsyncMock(
            return_value={"messages": [Mock(content="Result", tool_calls=[])]}
        )

        run = await RecipeRunner(recipe, values, user, graph=mock_graph).execute_async()

        assert run is not None
        assert isinstance(run, RecipeRun)
        assert run.recipe == recipe
        assert run.variable_values == values
        assert run.run_by == user

    @pytest.mark.asyncio
    async def test_recipe_runner_executes_prompt(self, recipe, user, recipe_step_1):
        """RecipeRunner records a single executed step on success."""
        from apps.recipes.services.runner import RecipeRunner

        values = {"region": "West", "limit": 15, "start_date": "2024-06-01"}
        mock_graph = Mock()
        mock_graph.ainvoke = AsyncMock(
            return_value={"messages": [Mock(content="Mocked response", tool_calls=[])]}
        )

        run = await RecipeRunner(recipe, values, user, graph=mock_graph).execute_async()

        assert len(run.step_results) == 1
        assert run.step_results[0]["step_order"] == 1
        assert run.step_results[0]["success"] is True

    @pytest.mark.asyncio
    async def test_recipe_runner_substitutes_variables_in_prompts(
        self, recipe, user, recipe_step_1
    ):
        """RecipeRunner renders variable values into the prompt."""
        from apps.recipes.services.runner import RecipeRunner

        values = {"region": "East", "limit": 25, "start_date": "2024-03-01"}
        mock_graph = Mock()
        mock_graph.ainvoke = AsyncMock(
            return_value={"messages": [Mock(content="Mocked response", tool_calls=[])]}
        )

        run = await RecipeRunner(recipe, values, user, graph=mock_graph).execute_async()

        step_result = run.step_results[0]
        assert "East" in step_result["prompt"]
        assert "25" in step_result["prompt"]

    @pytest.mark.asyncio
    async def test_recipe_runner_handles_execution_failure(self, recipe, user, recipe_step_1):
        """RecipeRunner records a failed run when the graph raises."""
        from apps.recipes.services.runner import RecipeRunner

        values = {"region": "North", "limit": 10, "start_date": "2024-01-01"}
        mock_graph = Mock()
        mock_graph.ainvoke = AsyncMock(side_effect=Exception("Agent execution failed"))

        run = await RecipeRunner(recipe, values, user, graph=mock_graph).execute_async()

        assert run.status == RecipeRunStatus.FAILED
        assert len(run.step_results) > 0
        assert run.step_results[0]["success"] is False
        assert "error" in run.step_results[0]

    @pytest.mark.asyncio
    async def test_recipe_runner_updates_run_status(self, recipe, user, recipe_step_1):
        """RecipeRunner marks the run completed with a completion timestamp."""
        from apps.recipes.services.runner import RecipeRunner

        values = {"region": "South", "limit": 5, "start_date": "2024-02-01"}
        mock_graph = Mock()
        mock_graph.ainvoke = AsyncMock(
            return_value={"messages": [Mock(content="Success", tool_calls=[])]}
        )

        run = await RecipeRunner(recipe, values, user, graph=mock_graph).execute_async()

        assert run.status == RecipeRunStatus.COMPLETED
        assert run.completed_at is not None
```

- [ ] **Step 3: Ensure `AsyncMock` is imported in `tests/test_recipes.py`**

The file's top import is `from unittest.mock import Mock, patch`. Change it to:

```python
from unittest.mock import AsyncMock, Mock, patch
```

- [ ] **Step 4: Run the runner tests**

Run: `uv run pytest tests/test_recipes.py::TestRecipeRunner tests/test_recipe_runner.py -v`
Expected: PASS (6 migrated unit tests + 1 contract test).

- [ ] **Step 5: Commit**

```bash
git add apps/recipes/services/runner.py tests/test_recipes.py
git commit -m "refactor: drop sync recipe execute() path, migrate tests to async (arch #238)

Delete the sync execute() and _create_run_record() (async_to_sync is
forbidden by the async-first convention); execute_async is now the single
live path. Migrate the mocked TestRecipeRunner unit tests to await
execute_async with a provided mock graph.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: Convert `RecipeRunView` to a raw async Django view

**Files:**
- Modify: `apps/recipes/api/views.py`
- Modify: `apps/recipes/urls.py`
- Test: `tests/test_recipes.py` (new async view tests)

- [ ] **Step 1: Write the failing async view tests**

Append to `tests/test_recipes.py`:

```python
@pytest.mark.django_db(transaction=True)
class TestRecipeRunView:
    """Tests for the async recipe run endpoint."""

    @pytest.mark.asyncio
    async def test_run_endpoint_returns_201_with_real_graph(self, recipe, user, recipe_step_1):
        """POST run/ builds the real graph (LLM mocked, no MCP tools) and returns 201."""
        from langchain_core.messages import AIMessage

        async def fake_ainvoke(messages, *args, **kwargs):
            return AIMessage(content="done", id="ai-1")

        mock_bound = MagicMock()
        mock_bound.ainvoke = AsyncMock(side_effect=fake_ainvoke)
        mock_llm = MagicMock()
        mock_llm.bind_tools.return_value = mock_bound

        client = AsyncClient()
        await sync_to_async(client.login)(email="test@example.com", password="testpass123")

        url = f"/api/workspaces/{recipe.workspace_id}/recipes/{recipe.id}/run/"
        body = {"variable_values": {"region": "North", "limit": 10, "start_date": "2024-01-01"}}

        with (
            patch(
                "apps.recipes.services.runner.get_mcp_tools",
                new=AsyncMock(return_value=[]),
            ),
            patch("apps.agents.graph.base.ChatAnthropic", return_value=mock_llm),
        ):
            resp = await client.post(url, data=body, content_type="application/json")

        assert resp.status_code == 201, resp.content
        data = resp.json()
        assert data["status"] == RecipeRunStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_run_endpoint_forbids_non_member(self, recipe, other_user, recipe_step_1):
        """A user with no workspace membership gets 403."""
        client = AsyncClient()
        await sync_to_async(client.login)(email="other@example.com", password="otherpass123")
        url = f"/api/workspaces/{recipe.workspace_id}/recipes/{recipe.id}/run/"
        resp = await client.post(url, data={"variable_values": {}}, content_type="application/json")
        assert resp.status_code == 403
```

Add these imports at the top of `tests/test_recipes.py` (the `from unittest.mock import …` line already exists; add `MagicMock`, and the rest):

```python
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from asgiref.sync import sync_to_async
from django.test import AsyncClient

from apps.recipes.services.runner import RecipeRunner, VariableValidationError
```

Also, while here, **fix the code-review Minor #4 from Task 2**: hoist the per-test inline `from apps.recipes.services.runner import RecipeRunner` (and `VariableValidationError`) statements out of the migrated `TestRecipeRunner` methods, relying on the new module-level import above (CLAUDE.md: imports at module level, never inside function bodies). Remove the now-redundant inline `from apps.recipes.services.runner import ...` lines inside those test methods. (`tests/test_recipe_runner.py` already imports `RecipeRunner` at module level with no circular-import issue, confirming the hoist is safe.)

- [ ] **Step 2: Run to verify the 201 test FAILS (sync view can't run the async path correctly)**

Run: `uv run pytest "tests/test_recipes.py::TestRecipeRunView" -v`
Expected: FAIL — the current sync `RecipeRunView.post` calls `runner.execute()`, which was deleted in Task 2 (`AttributeError`), so the endpoint returns 500. (This is the Critical the Task-2 code review flagged; Task 4 fixes it.)

- [ ] **Step 3: Replace `RecipeRunView` with `recipe_run_view` in `apps/recipes/api/views.py`**

Update the imports at the top of the file to add (keep the existing DRF imports used by the other views):

```python
import json

from asgiref.sync import sync_to_async
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_protect

from apps.recipes.services.runner import RecipeRunner
from apps.users.decorators import async_login_required
from apps.workspaces.services.workspace_service import touch_workspace_schemas
from apps.workspaces.workspace_resolver import aresolve_workspace
```

Delete the entire `class RecipeRunView(APIView):` block and replace it with:

```python
@csrf_protect
@async_login_required
async def recipe_run_view(request, workspace_id, recipe_id):
    """POST /api/workspaces/<workspace_id>/recipes/<recipe_id>/run/

    Execute a recipe with variable values. Raw async Django view (DRF APIView
    is sync and cannot await the async-first runner).
    """
    if request.method != "POST":
        return JsonResponse({"error": "Method not allowed"}, status=405)

    user = request._authenticated_user

    workspace, err = await aresolve_workspace(user, workspace_id)
    if err:
        return err

    try:
        recipe = await Recipe.objects.select_related("workspace").aget(
            pk=recipe_id, workspace=workspace
        )
    except Recipe.DoesNotExist:
        return JsonResponse({"error": "Recipe not found."}, status=404)

    try:
        body = json.loads(request.body) if request.body else {}
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    serializer = RunRecipeSerializer(data=body)
    if not await sync_to_async(serializer.is_valid)():
        return JsonResponse(serializer.errors, status=400)
    variable_values = serializer.validated_data.get("variable_values", {})

    try:
        runner = RecipeRunner(recipe=recipe, variable_values=variable_values, user=user)
        run = await runner.execute_async()
    except VariableValidationError as e:
        return JsonResponse({"error": str(e), "errors": e.errors}, status=400)
    except Exception as e:
        logger.exception("Error running recipe %s", recipe_id)
        return JsonResponse({"error": str(e)}, status=500)

    # Reset the inactivity TTL on user-initiated recipe runs.
    await touch_workspace_schemas(workspace)

    data = await sync_to_async(lambda: RecipeRunSerializer(run).data)()
    return JsonResponse(data, status=201)
```

Add `VariableValidationError` to the runner import so the `except` works:

```python
from apps.recipes.services.runner import RecipeRunner, VariableValidationError
```

- [ ] **Step 4: Point the URL at the function in `apps/recipes/urls.py`**

Change the import `RecipeRunView` → `recipe_run_view` in the `from .api.views import (...)` block, and change the run path from:

```python
    path("<uuid:recipe_id>/run/", RecipeRunView.as_view(), name="run"),
```

to:

```python
    path("<uuid:recipe_id>/run/", recipe_run_view, name="run"),
```

- [ ] **Step 5: Run the view tests — expect PASS**

Run: `uv run pytest "tests/test_recipes.py::TestRecipeRunView" -v`
Expected: PASS (201 + 403).

- [ ] **Step 6: Commit**

```bash
git add apps/recipes/api/views.py apps/recipes/urls.py tests/test_recipes.py
git commit -m "feat: make recipe run endpoint a raw async Django view (arch #238)

Convert RecipeRunView (DRF APIView, which can't await the async-first
runner) to an async def view mirroring chat_view: @async_login_required +
@csrf_protect, aresolve_workspace, async ORM recipe lookup, await
execute_async, async TTL touch, serializer wrapped in sync_to_async.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: Full verification

**Files:** none (verification only)

- [ ] **Step 1: Full recipe test suite**

Run: `uv run pytest tests/test_recipes.py tests/test_recipe_runner.py -v`
Expected: all pass, 0 skipped in `test_recipe_runner.py` (skeleton replaced).

- [ ] **Step 2: Lint + format**

Run: `uv run ruff check apps/recipes tests/test_recipes.py tests/test_recipe_runner.py && uv run ruff format --check apps/recipes`
Expected: clean. Fix any unused-import (e.g. confirm `status`, `APIView`, `Response` are still used by the remaining DRF views — they are — and that nothing references the removed `WorkspaceTenant`/`TenantMembership`/`async_to_sync`).

- [ ] **Step 3: No unintended migrations**

Run: `uv run python manage.py makemigrations --check --dry-run`
Expected: "No changes detected" (no model changes were made).

- [ ] **Step 4: Broader regression sweep (touched seams)**

Run: `uv run pytest tests/test_recipes.py tests/test_recipe_runner.py tests/test_dangling_tool_calls.py -q`
Expected: all pass (confirms the shared `build_agent_graph` contract still holds).

- [ ] **Step 5: Manual end-to-end (required before "done")**

Start deps + servers (`docker compose up platform-db mcp-server`, then `uv run honcho -f Procfile.dev start`), ensure a real `ANTHROPIC_API_KEY` is set, and run a real recipe via the UI or a `curl` POST to `/api/workspaces/<ws>/recipes/<id>/run/`. Confirm the response is a real, non-empty answer and `status == "completed"` with sensible `tools_used`. Capture the output for the PR description.

---

## Self-review notes

- **Spec coverage:** drift #1 (Task 2 step 3), #2 (Task 2 step 4), #3 (Task 2 step 3), #4 (Tasks 3+4); unmocked test (Task 1); migrated tests (Task 3); async view (Task 4); oauth_tokens kept + no Langfuse (Task 2 steps 3-4); manual e2e (Task 5 step 5). All covered.
- **Type/name consistency:** `recipe_run_view`, `RecipeRunner.execute_async`, `_oauth_tokens`, `RunRecipeSerializer`/`RecipeRunSerializer`, `aresolve_workspace`, `touch_workspace_schemas`, `get_mcp_tools`/`get_user_oauth_tokens` all used consistently across tasks.
- **No background-task / no PR-#277 changes** — scope boundary honored.
