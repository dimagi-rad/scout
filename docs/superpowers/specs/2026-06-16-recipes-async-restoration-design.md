# Recipe runner async-first restoration (arch #238)

**Date:** 2026-06-16
**Issue:** arch-review #238 — "Recipe runner signature drift — feature 100% dead since March"
**Wave/tier:** Wave 1, tier:now, status:broken-now
**PR closes:** #238, #276 (Sentry SCOUT-DJANGO-1P `SynchronousOnlyOperation`)

## Problem

Recipe execution has been 100% broken since a March refactor (commit `e26cd75`). The
runner and run view drifted away from the agent-graph contract and were never caught
because every recipe test mocks `build_agent_graph`, masking the break. Sentry #276
(`SynchronousOnlyOperation`) is the first crash users hit — a symptom of #238, not a
separate bug.

### Verified drifts (against current `main`)

1. **`_build_graph` signature drift** — `apps/recipes/services/runner.py` calls
   `build_agent_graph(tenant_membership=…, user=…, checkpointer=None)`. The real
   signature (`apps/agents/graph/base.py:480`) is
   `build_agent_graph(workspace, user=None, checkpointer=None, mcp_tools=None, oauth_tokens=None)`
   — no `tenant_membership`, `workspace` required. → `TypeError`.
2. **`initial_state` key drift** — uses `tenant_id` / `tenant_name` /
   `tenant_membership_id`. `AgentState` (`apps/agents/graph/state.py:80`) requires
   `workspace_id`, `user_id`, `user_role`, `thread_id`. Wrong keys → the injecting tool
   node reads `workspace_id=""` → every MCP data tool fails server-side validation.
3. **`mcp_tools` never loaded or passed** — the graph is built without tools, so the
   agent has no data access even if the signature were correct.
4. **Sync execution path** — `RecipeRunView` (DRF `APIView`) calls the sync `execute()`,
   which does `async_to_sync(self._build_graph)()` then sync `graph.invoke()`. This
   violates the codebase's async-first convention and is the proximate source of the
   `SynchronousOnlyOperation` (lazy FK / sync-ORM-in-async).

## Required approach (non-negotiable)

Async-first, per `CLAUDE.md` async conventions, the sync→native-async migration history,
and the enforcement guardrail. **No `async_to_sync`.**

- Delete the sync `execute()` path and the `async_to_sync` import.
- `execute_async` becomes the single live path (it has drifts 1–3 too — fix there).
- `RecipeRunView` becomes a raw `async def` Django view that `await`s `execute_async`,
  mirroring the chat/tenant async endpoints. DRF `APIView` is sync; it cannot stay.

### Scope boundary

- Request-scoped async restoration. **Do NOT** move recipe execution to a background
  Procrastinate task — that redesign is owned by #267.
- #266 lists `RecipeRunner.execute_async` as dead code to delete. That is **superseded**
  here — we promote it to the live path. Keep it.
- Do NOT touch / comment on PR #277, #276, or #266. This PR supersedes #277.

## Design

### 1. `apps/recipes/services/runner.py`

- **Remove** `from asgiref.sync import async_to_sync` and the entire `execute()` method.
- **`_build_graph`** rewritten to mirror chat (`apps/chat/views.py:153-185`):
  - Load the `Workspace` by FK id via async ORM:
    `ws = await Workspace.objects.aget(id=self.recipe.workspace_id)`. Using the stored
    FK id (no lazy `recipe.workspace` traversal) is what prevents the
    `SynchronousOnlyOperation` regardless of how the recipe was loaded (the insight
    behind jjackson's PR #277, generalised).
  - `mcp_tools = await get_mcp_tools()` and
    `oauth_tokens = await get_user_oauth_tokens(self.user)`.
  - `self._graph = await build_agent_graph(workspace=ws, user=self.user, checkpointer=None, mcp_tools=mcp_tools, oauth_tokens=oauth_tokens)`.
  - Drop all `tenant_membership` loading and the `self._tenant_membership` attribute —
    not needed by the new signature or state. `user_role` stays `"analyst"` (same as
    chat and the pre-break code). Store `oauth_tokens` on the instance for config use.
- **`execute_async`** `initial_state` fixed to real `AgentState` keys:
  `{"messages": [HumanMessage(...)], "workspace_id": str(ws.id), "user_id": str(user.id), "user_role": "analyst", "thread_id": self._thread_id}`.
  Remove the now-dead `WorkspaceTenant` tenant fetch and the `tenant_*` keys.
- **Config** mirrors chat:
  `{"configurable": {"thread_id": self._thread_id}, "recursion_limit": 50, "oauth_tokens": self._oauth_tokens}`.

#### Design note: `oauth_tokens` (kept) and Langfuse (excluded)

Two independent things ride in chat's `config`. We copy only one:

- **`config["oauth_tokens"]` — KEPT.** Fetched per-run from the running user via
  `get_user_oauth_tokens(self.user)`; ride the LangGraph runtime config →
  `langchain_mcp_adapters` forwards them into each MCP tool call's `_meta` (transport
  layer) → the MCP server reads them via `extract_oauth_tokens` (`mcp_server/auth.py:25`).
  They are **never visible to the LLM** and are **scrubbed from audit logs**
  (`mcp_server/envelope.py:82` `_SCRUB_KEYS`). The only consumer is `run_materialization`
  (data refresh). Tokens are **never stored** on the `Recipe` or `RecipeRun`: a shared
  recipe carries only its prompt + variables, so whoever runs it authenticates as
  *themselves* (their tokens, their membership). Sharing is therefore secure by
  construction; a user without CommCare-linked tokens simply can't trigger an in-recipe
  refresh — identical to chat.
- **`config["callbacks"]` (Langfuse tracing) — EXCLUDED.** That is chat-specific; the
  recipe runner attaches no Langfuse callback.
- **Imports:** add `get_mcp_tools`, `get_user_oauth_tokens` from `apps.agents.mcp_client`
  and `Workspace` from `apps.workspaces.models`. Remove `WorkspaceTenant` /
  `TenantMembership` if they become unused.

### 2. `apps/recipes/api/views.py` + `apps/recipes/urls.py`

- Replace the `RecipeRunView` DRF `APIView` with a raw async function view:
  `@csrf_protect @async_login_required async def recipe_run_view(request, workspace_id, recipe_id)`.
  - `user = request._authenticated_user`.
  - `workspace, err = await aresolve_workspace(user, workspace_id)` → 403 `JsonResponse` on err.
  - `recipe = await Recipe.objects.select_related("workspace").aget(pk=recipe_id, workspace=workspace)`
    → 404 `JsonResponse` on `Recipe.DoesNotExist`.
  - Parse JSON body; validate `variable_values` (reuse `RunRecipeSerializer` via
    `sync_to_async`, or inline) → 400 on invalid.
  - `run = await RecipeRunner(recipe=recipe, variable_values=…, user=user).execute_async()`.
    Wrap in try/except → 500 `JsonResponse` with `{"error": str(e)}` (same envelope).
  - `await touch_workspace_schemas(workspace)` (replaces the inline sync TTL-touch).
  - Return `JsonResponse(await sync_to_async(lambda: RecipeRunSerializer(run).data)(), status=201)`.
    `RecipeRunSerializer` only serializes scalar `RecipeRun` columns (no FK traversal),
    but DRF `.data` is sync, so it is wrapped in `sync_to_async`.
- The other five recipe views stay DRF `APIView` — only `run/` becomes async.
- `apps/recipes/urls.py`: point the `run/` path at `recipe_run_view` instead of `.as_view()`.

### 3. Tests

- **Migrate** the six mocked `TestRecipeRunner` tests (`tests/test_recipes.py:584+`) from
  sync `execute()` → async `execute_async()` (`@pytest.mark.asyncio` +
  `@pytest.mark.django_db(transaction=True)`), since `execute()` is removed. They keep
  mocking `build_agent_graph` — they are unit tests of validation / record creation /
  result extraction. `mock_graph.ainvoke` (AsyncMock) replaces `mock_graph.invoke`.
- **Add the real unmocked contract test** (the guardrail that would have caught #238):
  stand up the real in-memory MCP wire
  (`mcp.shared.memory.create_connected_server_and_client_session` over
  `mcp_server.server.mcp`, tools via `langchain_mcp_adapters.load_mcp_tools`), patch
  `apps.recipes.services.runner.get_mcp_tools` to return those real tools, and patch
  `apps.agents.graph.base.ChatAnthropic` with a fake LLM (pattern from
  `tests/test_dangling_tool_calls.py:153`) that emits a `get_schema_status` tool call,
  then a final `AIMessage` echoing the tool envelope. `build_agent_graph` is **not**
  mocked. Run `execute_async()` and assert:
  - `run.status == COMPLETED` (proves drift #1: the real build signature is satisfied);
  - the captured response contains `not_provisioned` and **not** `VALIDATION_ERROR`
    (proves drifts #2/#3: `workspace_id` flowed from state through the real injecting
    node into the real MCP server, with real tools attached).
  This mirrors the arch #234 / PR #277 unmocked pattern (`tests/test_chat_mcp_contract.py`,
  commit `2320d52`, not yet on `main`).
- **Replace** the `tests/test_recipe_runner.py` skeleton skips with the real contract
  test above (or co-locate it there; final placement decided in the plan).
- **Add an async view test**: `POST …/recipes/<id>/run/` end-to-end with the LLM mocked,
  asserting `201` + a serialized run body, and that a non-member gets `403`.

## Error handling

| Condition | Response |
|---|---|
| Unauthenticated | 401 (`@async_login_required`) |
| Not a workspace member | 403 (`aresolve_workspace`) |
| Recipe not found in workspace | 404 |
| Invalid/missing variable values | 400 |
| Runner raises (graph build / invoke) | 500, `{"error": str(e)}`, logged via `logger.exception` |
| Success | 201, `RecipeRunSerializer(run).data` |

A graph-level failure inside `execute_async` is still captured into
`step_results[0]["error"]` with `run.status = FAILED` and returned as a 201 (the run
record exists and records the failure) — matching existing behaviour. Only failures
*outside* the run record (resolution, validation, build) surface as 4xx/5xx.

## Verification before "done"

1. `uv run pytest tests/test_recipes.py tests/test_recipe_runner.py` green, including the
   new unmocked contract test.
2. Full async-ORM-in-async-view sanity: no `SynchronousOnlyOperation` under the async view.
3. **End-to-end**: run a real recipe locally (real MCP server + real `ANTHROPIC_API_KEY`)
   and confirm it returns a real answer — not just green unit tests.

## Out of scope

- Background-task execution (#267).
- Deleting `execute_async` (#266 — superseded).
- Any change to PR #277 / issues #276 / #266 coordination.
