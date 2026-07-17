# Lens: Error handling & silent fallbacks

*Architecture review v2, Phase 1 — cross-cutting lens reviewer.
Mandate: every `except`/`catch` that degrades quietly, every fallback chain, every
place a failure is converted into an empty list / default value / "completed" lie.
Ranked by how misleading the downstream symptom is. Report only — no code changes.*

Method: enumerated all 256 non-test `except` blocks in `apps/`, `mcp_server/`,
`config/` plus all 65 frontend `catch` blocks; traced the consequential ones
end-to-end (entry point → swallow site → what the user/agent is told instead).
The 2026-06-10 incident (d) — "view-schema build failures swallowed, agent told
completed" — is the template defect for this lens; the headline finding below is
its unfixed sibling one phase later in the same pipeline.

---

## F1. dbt transform failures are triple-swallowed: run ends COMPLETED, agent told "completed", zero UI surface

**Status: LATENT (deterministic when any dbt model fails) · Impact: correctness · Confidence: verified-by-trace · Complexity: accidental**

This is the exact sibling of incident 1d (PR #229 fixed it for the *view-schema*
phase; the *transform* phase still lies). Three independent swallow layers stack:

1. **`_execute_stage` does not raise on dbt failure.**
   `apps/transformations/services/executor.py:185`:
   ```python
   if not result.get("success"):
       logger.warning("Stage '%s' had failures: %s", stage_name, result.get("error"))
   ```
   `run_dbt` correctly returns `{"success": False, "error": ...}`
   (`mcp_server/services/dbt_runner.py:101-103`), per-asset rows are marked
   `FAILED` (`executor.py:172-174`) — and then the function returns normally.

2. **`run_transformation_pipeline` marks the run COMPLETED anyway.**
   Because step 1 didn't raise, the stage loop completes and
   `executor.py:81` sets `run.status = TransformationRunStatus.COMPLETED` with
   `error_message=""`. The `except` branch (`executor.py:84-91`, "Don't re-raise —
   transform failures are isolated from the data load") only fires for *real
   exceptions*, which step 1 just prevented. So the common failure mode (dbt model
   compile/runtime error) produces a TransformationRun that says COMPLETED.

3. **The materializer then reports a clean COMPLETED MaterializationRun.**
   `mcp_server/services/materializer.py:1060-1074` builds
   `{"status": run.status, ...}` and only attaches `"error"` if
   `run.error_message` is set — which it isn't (step 2). Back in `run_pipeline`,
   `materializer.py:497` reads `transform_result.get("error")` → `None`, so the
   run flips to `COMPLETED` (`materializer.py:471-477`) and the returned result
   has **no** `transform_error` key. (Even when an exception *does* bubble,
   `materializer.py:455-457` converts it to `{"error": str(e)}` and the run
   still goes COMPLETED — `transform_error` is buried in `run.result` JSON;
   `tests/test_materializer.py:443` pins exactly this buried-key behavior.)

**Downstream consequences of the lie:**

- The resume task derives status purely from run states and
  `result["sources"]` — it never reads `result["transforms"]`
  (`apps/workspaces/tasks.py:928-1016`, `_aggregate_materialization_state`).
  Status = `"completed"` → the agent is told
  *"Materialization just completed (status=completed). Please continue with the
  user's original request using the now-loaded data."* (`tasks.py:1121-1125`).
- ThreadJob terminal state = COMPLETED (`tasks.py:1229-1240`) → green success in
  the UI.
- The failed dbt models' tables don't exist, so catalog reconciliation
  (`mcp_server/services/metadata.py:97-101`) silently *removes them from
  `list_tables`* — the curated tables the user/agent relied on simply vanish,
  with every status indicator green. The data dictionary likewise shows nothing
  (dbt models absent, no error).
- Nothing in the frontend consumes transformation runs at all: `grep -rl
  transformations frontend/src` → zero files. `TransformationAssetRun.FAILED`
  rows are visible only via Django admin or the unconsumed
  `/api/transformations/runs/` DRF endpoint.
- `error_summary` composition for failed jobs also only reads
  `result["sources"]` (`tasks.py:64-122`), so even the failure-card path can
  never mention transforms.

**Reachable via:** every materialization of a tenant that has
`TransformationAsset` rows (`materializer.py:448-454`) — the chat
`run_materialization` tool, the retry endpoint, and `/refresh/`.

**Chain:** `run_materialization` (mcp_server/server.py:560) →
`materialize_workspace` (apps/workspaces/tasks.py:204) → `run_pipeline`
(materializer.py:452) → `_run_transform_phase` (materializer.py:1060) →
`run_transformation_pipeline` (executor.py:81) → `_execute_stage`
(executor.py:185 warning-only) → COMPLETED run (materializer.py:471) →
`_aggregate_materialization_state` ignores transforms (tasks.py:966) → agent
prompt "completed" (tasks.py:1121).

The "transform failures are isolated from the data load" *intent* (raw data
should survive a bad model) is essential; making the isolation invisible at
every aggregation layer is accidental.

---

## F2. Transient managed-DB failure makes the catalog lie — and tells the agent to re-run materialization

**Status: LATENT · Impact: cost-perf + correctness · Confidence: verified-by-trace · Complexity: accidental**

`_live_tables_in_schema` converts *any* query failure into an empty set:

- `mcp_server/services/metadata.py:151-157`: `except Exception:` →
  `"Could not enumerate live tables in schema %s; catalog will be empty"` →
  `return set()`. (Also `metadata.py:130-135` when `MANAGED_DATABASE_URL` unset.)

In `pipeline_list_tables` an empty live-set then does two contradictory things:

- every committed **raw source is excluded** (`metadata.py:85-87`:
  `if physical_name not in live_table_names: continue`);
- every **dbt model is included "optimistically"** (`metadata.py:98-101`:
  `if live_table_names and model_name not in live_table_names: continue` —
  falsy empty set skips the filter).

So one transient DB error yields either (a) a catalog of *only* dbt models with
all raw tables missing — a contradictory schema response of exactly the shape
that drove the #190 agent panic loops (hypothesis for that linkage, the
mechanism itself is traced) — or (b), for pipelines without dbt models, an
empty list that `list_tables` decorates with an explicit instruction:

- `mcp_server/server.py:163-166`:
  `"No completed materialization run found. Run run_materialization to load data."`

i.e. a DB blip is converted into the agent being told to launch a full
re-materialization (DROP/CREATE of every raw table, full provider re-export) of
data that is actually present and healthy. All inside a `success: True`
envelope — the agent has no way to distinguish this from truth.

The same helper backs the **data dictionary API**: `pipeline_list_tables` →
`DataDictionaryView._get_from_pipeline` returns
`Response({"tables": {}, "generated_at": None})` with HTTP 200
(`apps/workspaces/api/views.py:279-280`), and `_get_all_columns` returns `{}` on
any connection error (`api/views.py:94-96`) so tables can render with zero
columns — the UI shows "no data" / empty schema as a *successful* response.

**Reachable via:** MCP `list_tables` (every agent turn), data-dictionary page.

---

## F3. Chat thread-ownership check fails open on a broad `except`

**Status: LATENT · Impact: security · Confidence: strong-inference (mechanism verified; trigger requires a transient ORM error) · Complexity: accidental**

`apps/chat/views.py:121-124`:

```python
try:
    existing_thread = await Thread.objects.filter(id=thread_id).afirst()
except Exception:
    existing_thread = None
```

The comment says this exists because "a non-UUID thread_id cannot match any
row" — the *intended* catch is the UUID-validation error. But the broad
`except Exception` also swallows operational DB errors, and `existing_thread =
None` **skips the foreign-thread rejection** (`views.py:125-141`). The turn then
proceeds: `_upsert_thread` failure is independently swallowed
(`views.py:147-148`, "fire-and-forget on error"), and the LangGraph stream
writes user + assistant messages into the checkpointer keyed **only** by
`thread_id` (`views.py:188-196` config) — the checkpointer has no ownership
concept. Net: during a platform-DB blip (the class of event from the 2026-06-09
incident), a request carrying another user's thread UUID can append turns into
that user's conversation, and the rightful owner later sees them via
`_load_thread_messages`.

Narrow window (the same blip usually breaks the rest of the request), but this
is a security check deliberately converted to fail-open by an over-broad catch.
Fix shape: catch only `(ValueError, ValidationError)`; let operational errors
500.

**Reachable via:** `POST /api/chat/` with attacker-/accident-supplied
`threadId` in body.

---

## F4. OAuth credential fallback chain misdiagnoses every failure as someone else's

**Status: DEBT · Impact: correctness/velocity · Confidence: verified-by-trace · Complexity: accidental**

`apps/users/services/credential_resolver.py` collapses four distinct failures
into two indistinguishable outcomes:

1. **Decrypt failure → `None`** (`credential_resolver.py:83-85`): a Fernet key
   rotation / corrupt ciphertext is reported downstream as
   `"No credential configured"` (`apps/workspaces/tasks.py:266-272`) — the user
   is steered toward re-entering a key that *is* configured; the operator signal
   (key rotation broke all API-key connections) is one log line per tenant.
2. **Token refresh failure → keep using the stale token**
   (`credential_resolver.py:107-110`: `except TokenRefreshError:` →
   `"using existing token"`). The expired token then hits the provider and the
   run fails with `ConnectAuthError: HTTP 401` — surfaced as a materialization
   failure rather than "your connection expired, reconnect" — and there is no
   `AUTH_TOKEN_EXPIRED` envelope use on this path even though the code exists
   (`mcp_server/envelope.py:32`).
3. **OAuth team mismatch → `None`** (fail-closed by design, correct) — but
   downstream it is again indistinguishable from "no credential configured".
4. Same family: `adapters.decrypt_token` returns `""` on `InvalidToken`
   (`apps/users/adapters.py:48-52`), producing `Authorization: Bearer ` →
   provider 401 → misattributed auth failure.

Essential bit: fail-closed on mismatch. Accidental bit: all four collapse to
the same user-facing story.

---

## F5. Login-time tenant resolution failures are swallowed — user lands with zero data sources and no error

**Status: LATENT · Impact: correctness/velocity · Confidence: verified-by-trace · Complexity: accidental**

`apps/users/signals.py:65-78`: each provider's post-OAuth resolution
(`resolve_connect_opportunities` / `resolve_ocs_chatbots` /
`resolve_commcare_domains`) is wrapped in `except Exception: logger.warning`.
A provider-API blip at the moment of login yields a fully logged-in user with
**no TenantMembership rows** — so no workspace auto-creation, an empty
"data sources" page, and nothing anywhere telling the user (or support) that
resolution failed rather than "this account has no opportunities". The
`tenants/ensure/` endpoint can repair it, but only if someone knows to call it.
Swallowing here is half-essential (login must not fail because Connect is
down) — the missing half is surfacing "resolution failed, retry" state to the
client instead of an indistinguishable empty list.

---

## F6. Outage renders as empty history: checkpointer load → `[]` (HTTP 200), thread-list fetch → `threads: []` "loaded"

**Status: LATENT · Impact: correctness (perceived data loss) · Confidence: verified-by-trace · Complexity: accidental**

Two stacked swallows make a transient failure look like deleted conversations:

- Backend: `_load_thread_messages` returns `[]` on any checkpointer error
  (`apps/chat/thread_views.py:100-102`) — the messages endpoint then 200s with
  an empty list; the client cannot distinguish "empty thread" from "DB down".
- Frontend: `fetchThreads` catch sets `{ threads: [], threadsStatus: "loaded" }`
  (`frontend/src/store/uiSlice.ts:72-74`) — a failed list fetch renders as a
  successfully-loaded empty sidebar.

During the 22h worker/DB incident class, users would see their entire chat
history apparently gone. Same pattern in `repair_dangling_tool_calls`
(`apps/chat/helpers.py:39-41`, returns `[]`) — there the quiet fallback is more
defensible (a repair nicety) but it also means dangling tool_calls go unrepaired
exactly when the checkpointer is flaky, which is when they occur.

---

## F7. MCP `get_schema_status` returns "not_provisioned" for a nonexistent workspace

**Status: LATENT · Impact: correctness · Confidence: verified-by-trace · Complexity: accidental**

`mcp_server/server.py:667-686`: `Workspace.DoesNotExist` → the same
`success_response({"state": "not_provisioned", "tables": []})` used for a
genuinely-unprovisioned workspace. Since `workspace_id` is injected server-side
by the agent graph, `DoesNotExist` here means an internal contract violation
(stale id, cross-env id, injection bug) — and it is reported to the agent as
"no data yet, materialize away" instead of an error. Contrast: other tools
return `NOT_FOUND` for the same condition (`server.py:309`). One of these is
wrong; the silent one can trigger pointless materialization attempts against a
phantom workspace.

---

## F8. Pipeline-registry YAML failure silently deletes a provider

**Status: LATENT · Impact: correctness · Confidence: verified-by-trace · Complexity: accidental**

`mcp_server/pipeline_registry.py:88-96`: a pipeline file that fails to parse is
logged and omitted from the registry. Downstream, every workspace on that
provider gets `"No pipeline for provider 'X'"` (`apps/workspaces/tasks.py:254-262`,
`169-171`) — a misattributed symptom pointing at workspace configuration, when
the truth is "a deploy shipped a broken `connect_sync.yml`". A bad edit to one
of three YAML files turns an entire provider off quietly; nothing fails at
startup.

---

## F9. System-asset (staging SQL) generation failure: "continuing pipeline" — feeds F1

**Status: LATENT · Impact: correctness · Confidence: verified-by-trace · Complexity: mixed**

`mcp_server/services/materializer.py:230-234`: generation of system staging
assets from discovered metadata is `except Exception: logger.exception(...,
"continuing pipeline")`. Defensible in isolation (don't fail the load for a
codegen bug) — but the failure's only possible downstream manifestation is the
transform phase, where F1 guarantees it is *also* invisible. The two swallows
compose into: staging tables silently absent, run COMPLETED, catalog quietly
smaller.

---

## F10. Production-grade MemorySaver fallback still exported; DEBUG path caches it forever

**Status: DEBT · Impact: correctness (footgun) · Confidence: verified-by-trace · Complexity: accidental**

`apps/agents/memory/checkpointer.py:138-145` falls back to `MemorySaver()` on
*any* Postgres failure ("Conversations will NOT be persisted"). The live chat
path correctly does the opposite — `apps/chat/checkpointer.py:43-55` raises in
production and only falls back under `DEBUG` — and I found **no production
caller** of `get_postgres_checkpointer` (exported via
`apps/agents/memory/__init__.py`, used in tests). It is a loaded gun for the
next caller who imports the "official-looking" helper. Secondary: the DEBUG
fallback in `chat/checkpointer.py:48` is cached in the module global, so one
startup blip in a dev/staging-with-DEBUG environment silently routes *all*
subsequent conversations to process memory until restart.

---

## F11. Agent schema-context degradation is logged at DEBUG — invisible in prod

**Status: COSMETIC · Impact: velocity/observability · Confidence: verified-by-trace · Complexity: accidental**

`apps/agents/graph/base.py:284-287` (full-schema fetch fails → compact schema)
and `base.py:350-351` (multi-tenant table list fails → no table list in prompt)
both swallow with `logger.debug`. The agent silently runs with materially less
context — the kind of degradation that shows up as "the agent got dumb
yesterday" with nothing in the logs to correlate. Same class as F2's trigger
(both call into the managed DB per turn).

---

## F12. OCS writers map missing IDs to `""` TEXT primary keys — sibling of the fixed Connect missing-id bug

**Status: LATENT · Impact: correctness · Confidence: strong-inference (mechanism verified; depends on OCS payloads ever omitting ids) · Complexity: accidental**

`2587158` fixed the Connect writers' missing-id crash; the OCS siblings were
never audited (the seed predicted this). `mcp_server/services/materializer.py`
OCS writers use `.get(<id>, "")` for PRIMARY KEY columns:
`raw_experiments.experiment_id` (`materializer.py:875`),
`raw_sessions.session_id` (`materializer.py:927`), and the participants writer.
One id-less row silently inserts under PK `""` (quiet corruption); a second
crashes the whole source with an opaque duplicate-key error. Either outcome
misleads. (`ocs_sessions.py:50` already coerces `raw.get("id") or ""` upstream,
so the writer's `.get(..., "")` default is reachable.)

---

## Smaller / by-design items (inventory)

| Site | Behavior | Verdict |
|---|---|---|
| `apps/workspaces/tasks.py:386-392` | ThreadJob not visible after 3.75s backoff → warn + return; janitor catches up | Acknowledged race (tasks.py:373 TODO); logged loudly; acceptable interim |
| `tasks.py:919-925` `_persist_synthetic_failure_message` | swallow; "UX nicety, not correctness invariant" | Honest comment; fine |
| `tasks.py:1220-1227` updated_at bump | isolated swallow, documented consequence (green dot) | Fine |
| `materializer.py:626-629` cursor checkpoint persist | swallow → loses resume watermark only | Fine (re-loads more, no lie) |
| `schema_manager.py` role-drop best-effort (204, 464, 487, 507) | dangling `_ro` roles accumulate | DEBT, logged; suggest periodic sweep |
| `materialization_views.py:112`, `server.py:630` cancel/abort best-effort | logged; janitor backstop exists | Fine |
| `chat/stream.py:226-239` | generic "An error occurred" then `finishReason: "stop"` | User does see an error; finish-as-stop is mildly dishonest to the SDK but harmless |
| `frontend/src/store/domainSlice.ts:88-89` | ensure-tenant failure → `console.error` only | Minor: invisible to user, compounds F5 |
| `frontend LoginForm.tsx:29` `.catch(() => {})` | CSRF prefetch; next POST fails visibly | Fine |
| `workspaces/api/views.py` data-dictionary helpers | covered in F2 | — |
| `users/services/ocs_team.py:43-45` | detection failure → None, fail-closed guard downstream | By design (TODO OCS #3586); fine |
| `recipes/runner.py:236` etc. | agent exception → RecipeRun FAILED with error | Correctly surfaced |
| `artifacts/views.py:801-807` query-data | context error → per-query `error` entries, HTTP 200 | Errors are surfaced in-band; acceptable |
| `mcp_server/envelope.py tool_context` | does NOT catch tool exceptions — FastMCP surfaces them | Correct (no swallow) |

## What's actually fine (verified healthy)

- **Post-incident hardening in `tasks.py` is real**: pre-loop failures stamp a
  terminal FAILED run (`materializer.py:404-410`); `teardown_schema` reverts to
  ACTIVE on failed DROP rather than stranding data (`tasks.py:621-631`);
  `_procrastinate_job_status` explicitly refuses to conflate "couldn't tell"
  with "not active" (`tasks.py:693-725`) — a model of the *right* fallback
  semantics; `materialize_workspace` defers the resume in a `finally`
  (`tasks.py:356-360`) so no phantom spinner.
- **View-schema failure surfacing (#229) works as advertised**: build failure
  persists `last_error` (`schema_manager.py:427-431`), resume task inspects the
  row and refuses to claim success (`tasks.py:1073-1095`, terminal=FAILED at
  1229-1248).
- **`connect_base.py` is strict post-fix**: missing `results` key, bad JSON,
  non-2xx all raise typed errors with sentry-trace propagation.
- **`mcp_server/services/query.py`** returns classified error envelopes; no
  result-shaped lies.
- **Production checkpointer raises instead of degrading**
  (`apps/chat/checkpointer.py:49-55`).
- **Frontend WorkspaceDetailPage/ConnectionsPage** consistently map errors to
  visible state (13 catches, all `setError`-style).
- **Resume CAS chain** re-reads actual state instead of clobbering
  (`tasks.py:1258-1288`).

## Cross-cutting pattern

The codebase has two error-handling dialects. The *new* dialect (everything
touched by the May–June incident chain) is excellent: terminal states always
stamped, "unknown" kept distinct from "negative", failures threaded to the
agent prompt verbatim. The *old* dialect — transforms (F1), catalog helpers
(F2), login signals (F5), credential resolution (F4) — converts failures into
neutral-looking defaults (`[]`, `{}`, `None`, `COMPLETED`) at the point of
failure, and every aggregation layer above then launders the default into a
positive claim. Each incident so far (#185 phantom rows, #229 view-schema lie,
2026-06-10d) has been one instance of the old dialect getting promoted to prod
symptom. F1 and F2 are the two largest remaining instances and both sit on the
exact same materialization/catalog spine.

## Coverage log

**Deep-read (line-by-line for error paths):**
`apps/workspaces/tasks.py` (full), `mcp_server/services/materializer.py`
(orchestration + transform + completion + cursor + OCS/Connect writer sections;
not all 33 writers), `apps/transformations/services/executor.py` (full),
`mcp_server/services/metadata.py` (top half + `_live_tables_in_schema`),
`mcp_server/services/query.py`, `mcp_server/context.py` (full),
`mcp_server/envelope.py` (full), `mcp_server/pipeline_registry.py` (load path),
`mcp_server/auth.py` (full), `apps/chat/views.py` (lines 1–200),
`apps/chat/stream.py` (error paths), `apps/chat/checkpointer.py` (full),
`apps/chat/thread_views.py` (message-load path), `apps/chat/helpers.py` (repair
path), `apps/agents/memory/checkpointer.py` (fallback paths),
`apps/agents/mcp_client.py` (circuit breaker), `apps/users/services/credential_resolver.py`
(full), `apps/users/signals.py` (login handlers), `config/procrastinate.py`
(full), `mcp_server/loaders/connect_base.py` (pagination), key sections of
`mcp_server/server.py` (list_tables, run_materialization, get_schema_status,
status/cancel tools), `apps/workspaces/services/schema_manager.py` (teardown +
build_view_schema + provision), `apps/workspaces/api/views.py` (dictionary
helpers + view), `apps/workspaces/api/materialization_views.py` (cancel/retry),
`apps/workspaces/api/jobs_views.py` (reconcile backstop),
`apps/recipes/services/runner.py` (execute), `apps/users/adapters.py`
(decrypt), `apps/users/services/token_refresh.py` (refresh),
`apps/users/services/ocs_team.py` (detection).

**Skimmed (grep-level + targeted excerpts):** `apps/agents/graph/base.py`
(except sites only — the panic breaker and prompt assembly were NOT audited),
`apps/agents/tools/*.py` (tool-level catches only), `apps/artifacts/views.py`
(query-data path only; the other ~900 lines unexamined),
`mcp_server/loaders/ocs_*.py`, `commcare_*.py`, `connect_*` table loaders
(normalization greps only), `mcp_server/services/dbt_runner.py` (return shapes
only), frontend `store/*.ts`, `WorkspaceDetailPage`, `uiSlice`, `domainSlice`,
`authSlice`, `dictionarySlice`, `useWorkspaceJobs` (catch sites only),
`apps/knowledge/api/views.py`, `apps/transformations/services/lineage.py`
(except lines only).

**Not examined at all (honest gaps for the gap loop):**
`apps/users/services/merge.py`; `apps/users/services/tenant_resolution.py`
internals (only its callers); `apps/users/views.py` connection-CRUD error
paths; `mcp_server/services/sql_validator.py` internals (except-line only);
`apps/artifacts/services/export.py` (ImportError fallbacks unverified);
`apps/artifacts/views.py` sandbox/export/undelete paths;
`apps/recipes/api/views.py`; `apps/knowledge/services/retriever.py`;
`apps/chat/rate_limiting.py`, `message_converter.py`;
`apps/workspaces/api/workspace_views.py` (full), `jobs_cancel.py`,
`workspace_service.py`, management commands; `mcp_server/__main__.py`;
`apps/transformations/services/commcare_staging.py` and `dbt_project.py`;
frontend `ChatMessage`/`ChatPanel`/`ConnectionsPage` catch bodies,
`useEmbedMessaging`, `NetworkStatusContext`, public share pages; all of
`tests/` (i.e. which of these swallows are *pinned* by tests was checked only
for F1); `config/settings/*`; deploy configs.
