# Vertical review: Chat / Agent graph / Streaming / Checkpointer

*Reviewer: feature-vertical "chat-agent". Scope: chat views, streaming/SSE, the
LangGraph graph build, checkpointer, thread lifecycle, dangling-tool-call
handling, message growth/pruning, prompt assembly and caching. Report-only.*

Branch `main`, HEAD `35e4230`. Evidence standards per
`docs/arch-review-methodology.md`. Confidence labels per finding;
comments/docstrings treated as claims, not facts.

---

## Capability functionality matrix (what % actually works)

| Capability | Demo path | Integration edges | Notes |
|---|---|---|---|
| Single-tenant streaming chat (`POST /api/chat/`) | ~95% | ~85% | Core loop solid; SQL/tool **input never streamed live** (F4) |
| Multi-tenant streaming chat | ~90% | ~80% | View-schema state surfaced in prompt; same input-streaming gap |
| Thread lifecycle (create / list / messages / viewed / ownership) | ~95% | ~90% | Ownership checks are careful and correct |
| Dangling-tool-call repair | ~95% | ~90% | Double-guarded (helpers + agent_node); correct |
| Checkpointer (persistence) | ~90% | ~80% | Two parallel impls (F6); prod-fail behavior correct |
| Panic-loop escalation (#190 fix) | ~50% | ~50% | Detector works but **message is invisible live** (F1) |
| Resume-after-materialization | ~85% | ~75% | Heavy fix history; CAS/janitor correct; oauth dead (F2) |
| Prompt assembly + caching | ~80% | ~60% | In-proc assembly cache only; **no Anthropic prompt caching** (F3) |
| Message growth / pruning | n/a | **0%** | `prune_messages` is **dead code**; growth unbounded (F5) |
| Recipe → agent graph reuse | **0%** | **0%** | `build_agent_graph` call **signature drift — broken** (F7) |

---

## Findings

### F1 — Panic-loop escalation message is never streamed to the live UI (BROKEN-NOW, correctness)

**Confidence: strong-inference** (traced both files; not executed live).

The escalation node — the headline fix for agent panic loops (#190) — emits a
fixed message by returning a hardcoded `AIMessage`, not by calling the chat
model:

- `apps/agents/graph/base.py:619` `escalation_node` returns
  `{"messages": [AIMessage(content=ESCALATION_MESSAGE)]}`.
- `post_tools_router` routes `tools -> escalate` on a 3-error streak
  (`base.py:603-617`), and `escalate -> END` (`base.py:654`).

The SSE translator only forwards two event types:

- `apps/chat/stream.py:113` `if event_type == "on_chat_model_stream"` and
  `stream.py:159` `elif event_type == "on_tool_end"`. Nothing else is emitted.

Because `escalation_node` does **not** invoke the LLM, LangGraph never produces
an `on_chat_model_stream` event for it, so `ESCALATION_MESSAGE` is never turned
into a `text-delta`. During a live turn the user sees the failing tool cards,
then the stream just terminates with `finish` and **no explanatory text**. The
message *is* persisted to the checkpoint (it's a state update), so it appears
only after a page reload via `langchain_messages_to_ui`
(`apps/chat/message_converter.py:39`). The whole point of #190 — telling the user
"repeated schema errors… would you like me to re-materialize?" — does not reach
the user at the moment it fires. Normal agent text is unaffected (it flows
through `agent_node`'s `llm_with_tools.ainvoke`, which does emit
`on_chat_model_stream`).

**Chain:** `stream.py:97` `astream_events(...)` → loop handles only
`on_chat_model_stream`/`on_tool_end` (`stream.py:113,159`) → `escalation_node`
yields a non-model `AIMessage` (`base.py:619-621`) → no `text-delta` emitted →
stream closes at `stream.py:248-249` with the escalation text undelivered live.

**Impact:** correctness/velocity — the documented recovery affordance is silently
swallowed on the path it was built for. Essential vs accidental: **accidental**
(translator coupled to event *source* instead of message *role*).
Reachable_via: any chat turn that produces 3 consecutive `NOT_FOUND`/
`VALIDATION_ERROR` tool results.

---

### F2 — `oauth_tokens` plumbed end-to-end but consumed by nobody (DEBT, velocity)

**Confidence: verified-by-trace.**

OAuth tokens are loaded and threaded through the entire chat + resume path, then
dropped on the floor:

- Loaded: `apps/chat/views.py:162` `oauth_tokens = await get_user_oauth_tokens(user)`
  (`apps/agents/mcp_client.py:79`).
- Passed to graph: `views.py:172/184` `oauth_tokens=oauth_tokens` →
  `build_agent_graph(..., oauth_tokens=...)`. But in
  `apps/agents/graph/base.py` the parameter appears **only** at the signature
  (`base.py:485`) and docstring (`base.py:495`) — there is no reference to it in
  the function body (`grep oauth_tokens apps/agents/graph/base.py` → 2 hits, both
  non-code).
- Put in config: `views.py:196` `"oauth_tokens": oauth_tokens` and
  `tasks.py:1154` likewise. The injecting tool node injects only
  `workspace_id`/`user_id`/`thread_id`/`tool_call_id` into tool args
  (`base.py:504-512`) — **not** `oauth_tokens`, and into args, not `_meta`.
- MCP side: `mcp_server/auth.py:13` `extract_oauth_tokens(meta)` has **zero
  non-test callers** (only `tests/test_mcp_server.py`). `mcp_server/context.py:42`
  declares `oauth_tokens` but it is never assigned from a request, and no loader
  reads `ctx.oauth_tokens` (`grep` → only the declaration).

Materialization authenticates through a completely different path —
`aresolve_credential(membership)` (`apps/workspaces/tasks.py:160,264`) resolving
`TenantConnection` rows — so removing the oauth_tokens plumbing would not break
materialization. This is rename/migration residue from the
`TenantCredential → TenantConnection` switch (2026-06-05): the credential model
moved but the now-orphaned token-passing scaffolding survived.

**Impact:** velocity — dead scaffolding across `chat/views.py`,
`graph/base.py`, `tasks.py`, `mcp_server/auth.py`, `mcp_server/context.py` that
implies a working auth channel that does not exist; future readers will wire
against it. **Accidental** complexity. Reachable_via: every chat request builds
and threads it.

---

### F3 — No Anthropic prompt caching; full system prompt reprocessed every LLM call (DEBT, cost-perf)

**Confidence: verified-by-trace.**

`agent_node` rebuilds the message array as
`[SystemMessage(content=system_prompt), *repaired]` and calls
`llm_with_tools.ainvoke(messages)` on **every** iteration of the agent/tools loop
(`apps/agents/graph/base.py:580-581`). The system prompt is large — `BASE_SYSTEM_PROMPT`
(~7 KB) + `ARTIFACT_PROMPT_ADDITION` + workspace instructions + knowledge base +
a schema block budgeted to `SCHEMA_CONTEXT_CHAR_BUDGET = 6000`
(`base.py:81`). There is **no `cache_control` breakpoint** anywhere
(`grep -r cache_control|anthropic-beta apps mcp_server` → no hits in agent code),
and `ChatAnthropic` is constructed with only `model` + `max_tokens`
(`base.py:517-520`).

Consequence on Opus-class pricing: for a turn with N tool calls, the system
prompt + the entire (unbounded, see F5) message history is sent and reprocessed
N+1 times with zero cache hits. The in-process `_system_prompt_cache`
(`base.py:127`, 60 s TTL) only avoids **re-assembling** the string (saving DB
round-trips) — it does nothing for token reprocessing cost at the API.

**Impact:** cost-perf — the single largest controllable agent cost is left on the
table on the highest-churn file in the repo. **Accidental** complexity.
Reachable_via: every agent turn.

---

### F4 — Tool input (the SQL) is never sent during live streaming; only on reload (LATENT, correctness/velocity)

**Confidence: verified-by-trace.**

The live stream emits `tool-input-available` with a hardcoded empty input:

- `apps/chat/stream.py:191-198` → `{"type":"tool-input-available", ...,
  "input": {}}`. The args (e.g. the SQL passed to `query`) are never populated.

On a later reload, `message_converter.langchain_messages_to_ui` *does* populate
input from the persisted tool call: `apps/chat/message_converter.py:61`
`"input": tc.get("args", {})`. So the same tool card shows the SQL after reload
but is blank live.

This directly undercuts the base prompt's heavy provenance/"explain every query"
contract (`apps/agents/prompts/base_system.py:53-79`): while the agent is
running, the user cannot see *what* SQL is executing, only the eventual output.
The mismatch between the two renderers (live `{}` vs reload `tc.args`) is also a
contract drift inside this vertical.

**Impact:** correctness of the transparency story + velocity (debugging a live
run). **Accidental** complexity. Reachable_via: every tool call in a live chat
turn.

---

### F5 — `prune_messages` is dead code; conversation history grows unbounded (LATENT, cost-perf)

**Confidence: verified-by-trace.**

`apps/agents/graph/state.py:24` defines `prune_messages` and
`DEFAULT_MAX_MESSAGES = 20`, but **nothing calls it**
(`grep -rn 'prune_messages(' apps tests mcp_server` → only the docstring
example at `state.py:47`). `AgentState.messages` uses the bare `add_messages`
reducer (`state.py:138`) with no trimming, and neither `agent_node` nor the graph
wiring applies any cap. The only bound is `recursion_limit: 50` per turn
(`chat/views.py:195`), which caps tool *iterations*, not accumulated history.

Every turn loads the full checkpoint history and re-sends it (compounding F3).
Long-lived threads grow without limit until they approach the model context
window, at which point turns get slower and more expensive and eventually fail.
The careful tool-call-pair-preserving logic in `prune_messages`
(`state.py:71-77`) is wasted.

**Impact:** cost-perf, with an eventual correctness cliff on very long threads.
**Accidental** complexity (a built feature left unwired). Reachable_via: any
sufficiently long thread.

---

### F6 — Two divergent PostgreSQL checkpointer implementations (DEBT, velocity)

**Confidence: verified-by-trace.**

There are two checkpointer factories with different connection strategies and
different fallback semantics:

- `apps/chat/checkpointer.py:18` `ensure_checkpointer` — the **live** one. Lazy
  singleton over an `AsyncConnectionPool` (max_size 20, autocommit,
  `prepare_threshold=0`). In production it **raises** if Postgres is unavailable
  (`checkpointer.py:49-55`) — correct: never silently lose history in prod. Used
  by `chat/views.py`, `chat/thread_views.py`, `workspaces/tasks.py`.
- `apps/agents/memory/checkpointer.py:90` `get_postgres_checkpointer` — an
  `@asynccontextmanager` using `AsyncPostgresSaver.from_conn_string`, that
  **silently falls back to `MemorySaver`** on any failure
  (`memory/checkpointer.py:138-145`), plus `get_sync_checkpointer`
  (`:148`) that always returns `MemorySaver`. Only `get_database_url` from this
  module is actually imported by the live path
  (`chat/checkpointer.py:10`); the two factory functions are exported
  (`memory/__init__.py`) but not used in any runtime path.

The dead module's silent-`MemorySaver` behavior is the opposite of the live
module's prod policy. A future caller picking the "obvious" `memory` module
would reintroduce silent history loss that the live module was hardened against.

**Impact:** velocity + a latent correctness trap. **Accidental** complexity.
Reachable_via: not currently reachable at runtime (the factory functions);
`get_database_url` is reachable.

---

### F7 — Recipe runner calls `build_agent_graph` with a signature that no longer exists (BROKEN-NOW, correctness)

**Confidence: verified-by-trace.** *(Owner: recipes vertical; reported here as
contract drift on the graph-build boundary this vertical owns.)*

`build_agent_graph(workspace, user=None, checkpointer=None, mcp_tools=None,
oauth_tokens=None)` (`apps/agents/graph/base.py:480-486`) has **no
`tenant_membership` parameter and requires `workspace`**. Both recipe execution
paths call it with the old, pre-`workspaces`-rename shape:

- `apps/recipes/services/runner.py:115-119`
  `build_agent_graph(tenant_membership=self._tenant_membership, user=self.user,
  checkpointer=None)` — passes an unknown kwarg and omits the required
  `workspace`. This raises `TypeError` at call time.

Reached live by `POST /api/workspaces/<id>/recipes/<id>/run/` →
`RecipeRunList... RunRecipeView.post` → `RecipeRunner(...).execute()`
(`apps/recipes/api/views.py:107-108`) → `_build_graph` (`runner.py:99`).

Even if the signature were fixed, the `initial_state` it builds
(`runner.py:209-218`, `runner.py:301-313`) uses keys
`tenant_id`/`tenant_name`/`tenant_membership_id` that are **not** in `AgentState`
(`apps/agents/graph/state.py:80-148` defines `workspace_id`, `user_id`,
`user_role`, `thread_id`, `messages`). With `workspace_id` absent, the injecting
tool node would inject `""` (`base.py:461`) and every MCP data tool would fail —
so this is double contract drift (signature + state schema), both predating the
`projects → workspaces` rename.

The entire test suite for recipes mocks `build_agent_graph`
(`tests/test_recipes.py:599,623,...` all `@patch(...runner.build_agent_graph)`),
so the broken real call is never exercised — a textbook "mock hides the seam."

**Impact:** correctness — recipe run is non-functional against the real graph.
**Accidental** complexity. Reachable_via: Recipes UI "Run" → recipe run endpoint.

---

### F8 — Escalation/error detection couples to FastMCP's pretty-printed JSON substring (LATENT, correctness)

**Confidence: strong-inference** (verified serializer format; depends on SDK
behavior).

`_should_escalate` decides "panic loop" by substring-matching the tool content
against `'"code": "NOT_FOUND"'` / `'"code": "VALIDATION_ERROR"'`
(`apps/agents/graph/base.py:87,119-122`) — note the space after the colon. This
only matches because FastMCP serializes tool results with
`pydantic_core.to_json(result, indent=2)`
(`.venv/.../mcp/server/fastmcp/utilities/func_metadata.py:531`), which produces
`"code": "NOT_FOUND"`. I confirmed that compact serialization yields
`"code":"NOT_FOUND"` (no space), which would **not** match. So the panic-loop
circuit breaker silently stops working if the MCP SDK ever changes its result
serialization to compact form, or if an error path returns the envelope
unwrapped. The same brittleness applies to the prompt's `NOT_FOUND`/`VALIDATION_ERROR`
instructions (`base_system.py:130,139`) — prompt↔validator↔serializer coupling
across three layers with no shared constant.

**Impact:** correctness (a safety mechanism with a silent failure mode tied to a
third-party formatting detail). **Accidental** complexity. Reachable_via: panic
loop; degrades silently on SDK upgrade.

---

### F9 — Stream wall-clock timeout is best-effort; a single blocking await can blow past it, with no generator cleanup (LATENT, cost-perf)

**Confidence: strong-inference.**

`langgraph_to_ui_stream` enforces `AGENT_TIMEOUT_SECONDS = 300`
(`apps/chat/stream.py:39`) by checking a deadline **between** events
(`stream.py:100-104`). The check only fires when control returns from
`event_stream.__anext__()` (`stream.py:106`). If one LLM call or MCP tool blocks
for, say, 10 minutes, the deadline is not observed until the next event arrives,
so the "5-minute" cap can be substantially exceeded. On the `TimeoutError`/
`Exception` branches (`stream.py:212,226`) the underlying `event_stream`
async-generator is abandoned without `aclose()`, so an in-flight `ainvoke` (and
its Anthropic call) may keep running/billing until GC. A hard cap would need to
wrap each `__anext__` in its own `asyncio.wait_for` and explicitly close the
generator.

**Impact:** cost-perf + observability (orphaned billing, misleading "timed out"
message while work continues). **Accidental** complexity. Reachable_via: any
slow tool/LLM call.

---

### F10 — Rate limiting uses per-process LocMemCache; ineffective across API workers (LATENT, security/cost-perf)

**Confidence: verified-by-trace.**

`chat_rate_limit` (`apps/chat/rate_limiting.py:57`) reads/writes the Django cache
keyed `chat_rl:{user_id}`. The configured cache is
`LocMemCache` (`config/settings/base.py:318-323`) — explicitly per-process, with
an in-code NOTE that "rate limiting won't work across multiple workers"
(`base.py:325-326`). The two Kamal deploys (`config/deploy.yml` api,
`deploy-worker.yml`) and any multi-uvicorn-worker API process mean a user's
20-req/60s budget (`rate_limiting.py:15-16`) is multiplied by the worker count.
The limiter is correct (single atomic read-modify-write, `rate_limiting.py:28-54`)
but the backing store defeats it horizontally.

**Impact:** the chat abuse/cost guard is effectively `limit × num_workers`.
**Accidental** complexity (config, not logic). Reachable_via: every chat POST in
any multi-worker deployment.

---

### F11 — `resume_thread_after_materialization` and `chat_view` build *separate* in-process prompt caches in different processes (COSMETIC, cost-perf)

**Confidence: strong-inference.**

`_system_prompt_cache` is a module-global dict (`base.py:127`). The chat path runs
in the API process; the resume path runs in the worker process
(`tasks.py:1019` `resume_thread_after_materialization`,
`tasks.py:852 _build_agent_for_resume`). Each process keeps its own cache, and the
60 s TTL plus a key that omits schema/knowledge state
(`_system_prompt_cache_key`, `base.py:131-142`, keyed only on
workspace.id/user.id/system_prompt hash) means a freshly-materialized workspace
can still serve a "No data has been loaded yet" prompt for up to 60 s in the API
process after the worker has loaded data. The docstring acknowledges the staleness
window is intentional. Low impact given the short TTL and that the
post-materialization resume runs in the worker (fresh cache), but worth recording
as a known prompt-cache subtlety the mandate asked about.

**Impact:** cost-perf/cosmetic. **Essential**-ish (TTL caches trade staleness for
cost by design). Reachable_via: re-chat within 60 s of materialization in the
same API process.

---

## What's actually fine (verified healthy)

- **Thread-ownership enforcement** — `chat_view` rejects cross-user/cross-workspace
  thread reuse with a 404 (not 403, to avoid existence leaks)
  (`apps/chat/views.py:121-137`); `thread_messages_view` distinguishes
  brand-new vs stale/foreign threads correctly
  (`apps/chat/thread_views.py:146-156`); MCP `run_materialization` re-checks
  thread ownership as defense-in-depth (`mcp_server/server.py:563-570`). This is a
  careful, correct trust boundary.
- **Dangling tool-call repair** — both the pre-stream pass
  (`apps/chat/helpers.py:18-85`) and the in-graph guard
  (`apps/agents/graph/base.py:549-578`) correctly synthesize ToolMessages for
  unanswered `tool_use` ids before any Anthropic call, with the right
  reduce/idempotency shape. Redundant but defensively sound.
- **Resume CAS + janitor reconciliation** — the claim CAS over
  `CLAIMABLE_STATES = [PENDING, CANCELLED]` excluding RUNNING
  (`tasks.py:1037-1050`), the post-ainvoke state-scoped update guarding against a
  racing cancel (`tasks.py:1258-1288`), and the `_procrastinate_job_status`
  None-vs-active distinction (`tasks.py:693-725, 742-749`) are all carefully
  reasoned and match the 19-commit fix history. The API-side reconcile backstop
  (`apps/workspaces/api/jobs_views.py:117-135`) is a sound answer to the 22 h
  dead-worker incident.
- **View-schema build-failure surfacing** — the resume task inspects
  `WorkspaceViewSchema` directly and tells the agent NOT to re-materialize on a
  view-schema build failure (`tasks.py:1073-1095`), flipping the job to FAILED
  (`tasks.py:1229-1248`). This correctly closes the "told completed when broken"
  incident (1d).
- **`Thread.updated_at` upsert correctness** — the explicit `updated_at` in
  `defaults` (`chat/views.py:42-52`) is a real fix for the `auto_now`-skip on
  empty `update_fields`, and is well-documented.
- **Resume-marker hiding** — synthetic resume HumanMessages are filtered from the
  UI by prefix (`message_converter.py:13-20`, `constants.py:6`) so the user sees
  only the agent's reply.
- **Frontend cross-workspace thread recovery** — the 404→fresh-thread recovery in
  `ChatPanel` (`ChatPanel.tsx:101-112`) plus the synced-ref loop guard in
  `useWorkspaceThreadSync.ts:47-96` look correct and match the 00c423d fix.

---

## Coverage log (honest)

**Deep-read (line-by-line):**
- `apps/chat/views.py`, `helpers.py`, `stream.py`, `thread_views.py`, `models.py`,
  `message_converter.py`, `constants.py`, `rate_limiting.py`, `urls.py`,
  `checkpointer.py`
- `apps/agents/graph/base.py`, `graph/state.py`, `mcp_client.py`, `tracing.py`,
  `memory/checkpointer.py`
- `apps/workspaces/tasks.py` resume section (`852-1289`), janitor/reconcile
  (`640-839`), and `_defer_resume_for_job`/sibling-rebuild (`356-459`)
- `apps/workspaces/api/jobs_views.py`
- `apps/recipes/services/runner.py`; `apps/recipes/api/views.py` run path
- `apps/agents/prompts/base_system.py`
- `frontend/src/components/ChatPanel/ChatPanel.tsx`,
  `frontend/src/hooks/useWorkspaceThreadSync.ts`

**Skimmed (targeted grep / partial read):**
- `mcp_server/server.py` (`run_materialization`, `get_schema_status`, tool list),
  `mcp_server/auth.py`, `mcp_server/envelope.py`, `mcp_server/context.py` (oauth
  field), `mcp_server/services/sql_validator.py` + `query.py` (limit constants)
- `apps/agents/tools/artifact_tool.py` (signature/conversation_id), `learning_tool.py`,
  `recipe_tool.py` (outline only)
- `apps/agents/prompts/artifact_prompt.py` (size guidance only)
- `frontend/src/components/ChatMessage/ChatMessage.tsx` (tool-card / input
  rendering region), `apps/workspaces/services/workspace_service.py`
  (`touch_workspace_schemas`)
- `config/settings/base.py` (cache, model, resume, rate-limit settings)
- FastMCP serializer + `langchain_mcp_adapters/tools.py` (to confirm F8 and
  oauth `_meta` non-delivery)

**NOT examined (in-scope but not opened — drives the gap loop):**
- `frontend/src/contexts/WorkspaceJobsContext` and `hooks/useWorkspaceJobs.ts`
  internals (polling cadence, `recentlyCompletedThreadIds` derivation,
  `notifyJobLikelyStarted`) — I read only their consumption in `ChatPanel`.
- `frontend/src/components/ChatMessage/ToolOutput.tsx` and `SqlHighlighter.tsx`
  rendering details; `MaterializationProgressBanner` percentage logic.
- The embed/widget chat path (`useEmbedMessaging.ts`, `useEmbedParams.ts`,
  `/widget.js`) — not traced at all.
- `message_converter` reasoning/thinking-block handling and multi-part edge cases
  beyond text+tool pairing.
- Full `mcp_server/server.py` (other 8 tools) and the materializer — out of this
  vertical, owned by MCP/loader reviewers; I only confirmed the credential path
  for F2.
- `tests/test_agent_graph*.py`, `test_resume_thread_task.py`,
  `test_mcp_chat_integration.py` were not read in depth — I confirmed the recipe
  tests mock the graph (F7) but did not audit what the chat/resume tests mock vs
  exercise. A test-architecture pass on these is warranted.
- The `should_continue`/recursion-limit interaction with the `escalate` terminal
  edge under concurrent cancellation — reasoned about, not exhaustively traced.
- Langfuse callback behavior under real streaming (only read the wiring).
