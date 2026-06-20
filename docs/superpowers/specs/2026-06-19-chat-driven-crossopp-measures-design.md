# Chat-Driven Cross-Opp Measure Creation — Design

**Date:** 2026-06-19
**Status:** Draft for review
**Driving goal:** Make the cross-opp auto-model genuinely chat-driven. Today the canonical
measures are a hardcoded list seeded by the `build_crossopp_workspace` management command;
the chat agent cannot create or resolve measures. This wires the loop the demo *implies*:
you ask a cross-opp question, and if a needed measure isn't defined, Scout resolves it across
every opportunity, validates with you when it has doubt, commits it, and answers — with the
per-opp field/label/confidence/SQL one expand away, exactly like the `/crossopp` inspector
and like opening "what's underneath" an artifact.

## 0. Definition of done

In the **KMC Cross-Opp** workspace chat:

1. Ask a cross-opp question naming a domain concept that is **not** yet a canonical measure
   (e.g. "compare average length of stay and the referral rate across the opps").
2. Scout recognizes the missing measure, runs the per-opp resolver across all 11 opps, and:
   - **No doubt** (every opp resolves with confidence ≥ 0.5): commits silently and shows the
     measure's per-opp lineage inline (expandable).
   - **Doubt** (any opp low-confidence `<0.5` or absent): pauses with an **inline approval
     card** — each flagged opp shows the guessed field, a shortlist to pick a different field,
     and a reject (mark absent) action; confident opps are shown collapsed as auto-resolved.
3. On approval (with your per-opp overrides), Scout commits the measure (additively into the
   Cube model + lineage), the new measure becomes queryable, and the agent continues to answer.
4. Every number the answer uses can be expanded to its canonical definition, per-opp resolved
   field/label/confidence, and the exact SQL — inline in chat.
5. Defining a new measure **never** changes the identity or expressions of existing measures
   (model stability; Simon #303).

## 1. What exists today (reuse, don't rebuild)

- **The resolver (`apps/transformations/services/measure_resolver.py`)** — the genuine
  auto-model. `gather_measure_candidates(form_definitions)` flattens one opp's app structure
  into typed/labeled field candidates; `resolve_measure(spec, candidates)` is an LLM that picks
  the field by **label meaning** and returns `MeasureResolution{column, sql_expression,
  confidence, status, matched_label, reason}`. `low_confidence` is flagged below 0.5; absence
  is explicit. Per-opp, tested, deterministic substrate. **Reused unchanged.**
- **The cross-opp cube builder (`crossopp_cube_builder.py`)** — emits Tier-1 per-opp cubes +
  the Tier-2 blended `UNION ALL` cube from resolutions. **Reused.**
- **`build_crossopp_workspace` command** — currently the *only* caller: it loops a **hardcoded
  `STARTER_MEASURES`** list × opps, resolves, writes `cube/model/ws_<hash>/canonical.yml`,
  persists `CrossOppMeasureLineage`, provisions the role. Its core logic is what we extract.
- **`CrossOppMeasureLineage` (`apps/transformations/models.py`)** — per-(measure, opp) field /
  confidence / status / SQL. Already serves the `/crossopp` inspector. **Reused + written by
  the new service.**
- **Chat agent (`apps/agents/graph/base.py`, `apps/chat/`)** — LangGraph `agent → tools` loop,
  Postgres checkpointer, SSE stream. Tools today: `query`, `semantic_query`, `semantic_catalog`,
  table tools, `create_artifact`, `save_as_recipe`, `run_materialization`. No measure tool.
- **Fire-and-resume HITL precedent** — `run_materialization` ends the turn, a background job
  runs, and `resume_thread_after_materialization` (`apps/workspaces/tasks.py`) injects state via
  `agent.aupdate_state` and re-runs the agent. **This resume machinery is reused for approval.**
- **Per-tool output rendering (`frontend/.../ChatMessage.tsx`)** — `renderToolOutput` is a
  `switch(toolName)` registry; `query` already renders a collapsible card with
  `SqlHighlighter`. **The seam for an inline lineage/approval renderer already exists.**

The catalog of measures = the Cube model (per design: "no new Django measure model; Cube is
the catalog"). "Is this measure already defined?" is answered by `semantic_catalog`.

## 2. Architecture (Option B: inline approval via the proven resume pattern)

Rejected alternative — **native LangGraph `interrupt()`**: cleanest conceptually (the graph
truly pauses inside the tool) but requires new interrupt plumbing in the SSE translator
(`apps/chat/stream.py`), a resume request path, and frontend interrupt handling. Higher risk;
touches the streaming core. **Not this round.**

**Chosen — Option B:** the tool resolves and either commits or returns a `needs_approval`
payload and ends the turn. The approval card renders from the tool's *output* (existing
per-tool renderer seam). Approval is a side-channel API that commits and resumes the thread
via the **existing** `aupdate_state` + resume-task machinery. Real inline approval UI; the
risky streaming core is untouched.

## 3. Components

| # | Component | Path | Responsibility |
|---|---|---|---|
| 1 | `crossopp_measure_service` | `apps/transformations/services/crossopp_measure_service.py` (new) | Extract resolve → regenerate-model → persist-lineage → reload-cube from the command into a reusable service with an **incremental `add_measure`** path. Additive: regenerating the model preserves every existing measure's id + expression. `build_crossopp_workspace` becomes a thin caller. |
| 2 | `define_crossopp_measure` tool | `apps/agents/tools/crossopp_measure_tool.py` (new) | Agent tool. Checks catalog; loads each opp's `form_definitions`; runs the resolver across the workspace's opps; classifies doubt; **commits** (no doubt) or **drafts + `needs_approval`** (doubt). Workspace-scoped (only the workspace's own tenants). |
| 3 | `CrossOppMeasureDraft` model + approval API | `apps/transformations/models.py`, `apps/workspaces/api/crossopp_views.py` | Draft holds workspace, measure spec, the per-opp resolutions, flagged opps + shortlists, status, `thread_id`. `POST …/crossopp/measures/<draft>/approve` applies per-opp overrides → commit → resume thread. |
| 4 | Frontend renderer | `frontend/src/components/ChatMessage/CrossOppMeasureOutput.tsx` (new) + a case in `renderToolOutput` | `committed` → expandable per-opp lineage card (mirror `/crossopp` inspector + `SqlHighlighter`). `needs_approval` → approval card: per flagged opp confirm / pick-from-shortlist / reject; confident opps collapsed. Submits to the approval API; on success the thread resumes. |
| 5 | System-prompt guidance | `apps/agents/prompts/` | For a cross-opp question: consult `semantic_catalog`; if a needed measure is missing, call `define_crossopp_measure` **before** `semantic_query`. Name measures in plain domain language with a one-line description + kind (`numeric`/`rate`). |

## 4. Data flow

1. User asks a cross-opp question in the workspace chat.
2. Agent calls `semantic_catalog`; identifies needed concept(s) absent from the catalog.
3. Agent calls `define_crossopp_measure(name, description, kind)` per missing measure.
4. Tool loads each opp's `form_definitions`, runs `gather_measure_candidates` + `resolve_measure`
   → one `MeasureResolution` per opp.
5. Classify:
   - **No doubt** → commit via the service → return `{status: "committed", lineage:[...]}`.
   - **Doubt** → persist a `CrossOppMeasureDraft` (all resolutions + per-flagged-opp shortlists
     from `gather_measure_candidates`) → return `{status: "needs_approval", draft_id, flagged:[...]}`
     and end the turn.
6. Frontend renders committed-lineage or the approval card.
7. User submits per-opp choices → `POST …/crossopp/measures/<draft>/approve {overrides}`.
8. Approve: apply overrides → service commits → resume the thread (inject "measure `<name>` is
   now defined" + re-run agent), reusing the materialization resume task.
9. Agent resumes → `semantic_query` with the now-defined measure → answers; lineage expandable.

## 5. Commit step (the service contract)

`add_measure(workspace, spec, resolutions)`:
- Regenerate `cube/model/ws_<hash>/canonical.yml` **additively** — union of existing measures +
  the new one; existing Tier-1/Tier-2 measure entries are byte-stable except for the added
  measure. Diff-and-write, never regenerate-from-a-hardcoded-list.
- Upsert `CrossOppMeasureLineage` rows for `(measure, opp)`.
- **Make it queryable:** trigger a Cube model reload. *Build-time risk to resolve in the plan:*
  whether Cube dev-mode hot-reloads model files from the shared volume (preferred, no restart)
  or needs a reload signal / container restart. The commit owns this; on reload failure the
  measure is persisted but flagged "not yet queryable" and surfaced to the user.
- Idempotent: re-defining an existing measure updates only its expressions.

## 6. Isolation & stability

- **Isolation:** the tool only resolves/queries the *workspace's own* tenant schemas (unchanged
  posture; the least-privilege role and negative test from #302 still apply).
- **Stability (#303):** measure identity = the expert/agent-chosen name. `add_measure` is purely
  additive; a dedicated test asserts defining a new measure leaves every existing measure's id
  and SQL unchanged so artifacts / golden queries / saved comparisons keep working.

## 7. Error handling

- Resolver/LLM error on one opp → treat that opp as low-confidence (flag), never fail the whole
  measure.
- Cube reload failure → measure persisted, flagged not-yet-queryable, surfaced in chat.
- Abandoned draft → TTL cleanup; re-asking re-resolves fresh.
- Concurrent define of the same measure → idempotent, additive (last write wins).
- Agent over-eager to define → guarded by the catalog check + the doubt gate (no silent commit
  when uncertain).

## 8. Testing

- **Unit:** `add_measure` additive regen preserves existing measures (stability); doubt
  classification (all-confident vs any-low/absent); override application to a draft.
- **Unit:** tool returns the `committed` vs `needs_approval` shapes correctly (fake resolver).
- **Integration:** define a measure over 2 seeded tenants with divergently-named fields → cube
  model gains the measure, lineage persisted, `semantic_query` returns it per opp.
- **API:** approve endpoint applies overrides, commits, and triggers the resume task.
- **E2e (cube_e2e):** full loop — ask → resolve (force one opp low-confidence) → approve →
  `semantic_query` returns the measure across opps; **negative isolation** still holds.
- **Frontend:** renderer shows the lineage card (committed) and the approval controls
  (needs_approval) using the existing collapsible + `SqlHighlighter` pattern.

## 9. Scope / non-goals

- **In scope:** the chat-driven define→doubt-gate→approve→commit→answer loop for ONE cross-opp
  workspace; inline approval UI (confirm / pick-from-shortlist / reject per flagged opp);
  inline lineage rendering.
- **Non-goals:** native `interrupt()` plumbing (use resume pattern); multi-workspace /
  cross-workspace composition; free-form SQL editing in the approval UI (corrections are
  shortlist-pick or chat); auto-bootstrapping a whole catalog from one prompt (we create the
  measures a given question needs, on demand); improving synthetic data realism.
