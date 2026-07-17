# Architecture Review Methodology v2

*Status: proposal, 2026-06-12. Supersedes the single-agent `review_prompt.md` approach.
Designed from evidence: two independent v1 runs (`ARCHITECTURE_REVIEW.md`,
`docs/architecture-review-2026-06-12.md`) overlapped only ~60%, each found verified
S1-class issues the other missed, and each confidently misdescribed code it had read.*

## What v1 taught us (design inputs)

| Observation from runs 1 & 2 | Design consequence |
|---|---|
| 60% overlap; each found ~75% of the union | Multiple independent full passes are worth it; one is not enough |
| B deep-read `tasks.py` yet described the refresh path as working; A chased it and found data loss | Attention, not context size, is the bottleneck → coverage must be **assigned and logged**, not assumed |
| Both made confident misreadings (B's F5; B praising the role API that is unenforced) | Every finding needs **independent adversarial verification** before it enters the report |
| Everything found by *both* reviewers was real | Replication count is a free confidence signal → keep generalists fully independent |
| Both shared identical blind spots (ops, observability, dbt, tests-as-architecture, frontend) | Identical prompts → correlated gaps. Need structurally different mandates |
| The best findings were cross-subsystem contract drift (refresh↔pipeline, recipes↔graph signature, artifacts↔tenancy) | Seams and end-to-end journeys need **dedicated owners** |
| The "known symptom areas" section steered both reviewers well | Keep seeding known incidents/symptoms; harvest them automatically from git + postmortems |

## Design principles

1. **Three overlapping partitions of the codebase** — by *module*, by *feature*, and by
   *failure class (lens)* — so every line is in-scope for ≥3 reviewers approaching from
   different directions. Misses require three independent failures.
2. **Independence where it buys union, sharing where it buys depth.** Generalists see
   nothing from each other. Specialists see the cartography map (not each other's findings).
   Verifiers see one finding and nothing else.
3. **Findings are claims until verified.** No claim reaches the final report without an
   adversarial trace of the actual code path, including reachability (is it wired to a
   route/UI/tool?).
4. **Coverage is measured, not asserted.** Every reviewer emits a coverage log
   (deep-read / skimmed / skipped per file). The gap loop runs on the union of these logs.
5. **Loop until dry.** Stop when a full round of gap-targeted reviewers produces
   almost nothing new and verified — not after a fixed count.
6. **The roster is seeded, then discovered.** We specify a minimum roster; a cartography
   phase proposes additions based on what the codebase actually contains.

## Pipeline overview

```
Phase 0  Cartography ──► map + churn stats + seeded symptoms + proposed roster
Phase 1  Review fleet (parallel, independent):
           3 generalists · feature verticals · cross-cutting lenses
           seam reviewers · journey tracers · git historian
Phase 2  Adjudication: dedup/cluster → adversarial verify → contradiction resolution
Phase 3  Gap loop: coverage matrix + completeness critic → targeted round-2 reviewers
           → re-adjudicate → repeat until dry (≤2 new verified findings per round)
Phase 4  Synthesis: canonical report ← verified-findings DB + cartography + arch maps
           then a red-team pass on the report itself
```

All intermediate artifacts persist under `docs/arch-review/<date>/`
(`cartography/`, `reports/`, `findings/`, `verifications/`, `coverage/`, `synthesis.md`)
so the process is resumable and auditable.

---

## Phase 0 — Cartography (1–2 agents)

Produces the shared map that specialists receive (generalists do NOT receive it):

- **Module inventory**: every package/file with LOC and one-line responsibility.
- **Churn analysis**: full git log → fix-commit ratio, per-file touch counts, fix-chain
  clusters ("five commits orbiting one state machine"), rename/migration events and
  what they left behind.
- **Feature inventory**: every user-facing surface (HTTP routes, UI pages, agent tools,
  MCP tools, management commands, cron tasks) with an entry-point list.
- **Seam inventory**: process boundaries (API↔MCP↔worker↔frontend), stored-reference
  couplings (free-text SQL/prompts naming schema objects), shared-row writers
  (which modules write which state columns).
- **Symptom seeds**: harvest known incidents from git messages, PR titles, postmortem
  docs, TODO/FIXME/HACK comments.
- **Proposed roster**: starting from the minimum roster below, add/split/merge
  reviewers based on what it found (e.g., "transformations subsystem is bigger than
  expected — give it its own vertical"). The orchestrator approves the final roster;
  additions are cheap, so default to accepting them.

## Phase 1 — The review fleet (parallel, independent)

Every reviewer gets the **shared evidence standards** (below) and must return
(a) structured findings, (b) a coverage log, (c) a free-text report written to
`reports/`. Reviewers may use their own subagents internally; they may not see other
reviewers' output.

### A. Generalists ×3 (full codebase, fresh eyes)

Essentially `review_prompt.md` as-is — including the symptom seeds — with the evidence
standards added. No cartography map, no roster knowledge: their value is the
uncorrelated union and the replication signal. Three rather than two: the third
disproportionately grows the union when overlap is ~60%.

### B. Feature verticals (one per feature; minimum set for Scout)

Mandate: *own this feature completely.* Trace every entry point end-to-end, state what
% is actually functional (demo-path vs. integration edges), find dead paths, contract
drift, and unfinished seams. "Does this feature actually work, for every kind of
workspace/tenant/role that can reach it?"

1. Materialization & schema lifecycle (pipelines, TTL/janitors, refresh, cancellation, resume)
2. Artifacts (generation, sandbox, live queries, export, embed/widget SDK)
3. Recipes + knowledge + learnings (the agent-context periphery)
4. Chat / agent graph / streaming / checkpointer
5. Accounts, auth, OAuth providers, merge/reconciliation
6. Workspaces, tenancy, memberships, roles, sharing
7. Data dictionary / metadata / catalog
8. MCP server (tools, envelope, authz model)
9. Transformations / dbt (under-covered by both v1 runs)
10. Frontend (full app, not just the store layer)

### C. Cross-cutting lenses (one per failure class)

Mandate: *hunt one class of defect everywhere.* The lens list is the failure taxonomy
the v1 runs actually surfaced, plus the shared blind spots:

1. Data integrity & state machines (races, CAS, multi-writer rows, janitor interactions)
2. AuthZ & security surface (route Opus if Fable balks; frame as defensive audit of our own code)
3. Input validation & external-data boundaries (identifier length/shape, provider payloads)
4. Dead code, vestige & rename residue (zero-caller functions, stale docstrings, shims)
5. Consistency: same problem solved N ways (resolvers, catalogs, status derivation)
6. Error handling & silent fallbacks (every `except` that degrades quietly)
7. Test architecture (what do the mocks hide; which seams have zero real coverage)
8. Performance & cost (per-request rebuild costs, unbounded growth, prompt-cache behavior)
9. Ops/config/deployment (settings drift, secrets, docker vs prod parity, deploy-mid-job)
10. Observability (could we debug the next incident from what's emitted?)

### D. Seam reviewers (one per boundary — the interaction mandate)

Mandate: *own a contract, not a component.* Enumerate the implicit contract at the
boundary, then check both sides still agree — these are where every v1 headline
finding lived:

1. chat ↔ MCP ↔ worker (tool schemas, ThreadJob lifecycle, resume protocol)
2. materialization ↔ stored references (artifact SQL, knowledge, recipes vs. schema renames)
3. accounts ↔ tenancy ↔ workspace sharing (the chain the user must reason about as one thing)
4. backend ↔ frontend (API response shapes vs. hand-rolled TS types; UI affordances wired to nothing)
5. platform DB ↔ managed DB (Django state rows vs. physical schemas; who reconciles drift)

### E. Journey tracers ×2–3 (the global view)

Mandate: *follow a user, not a module.* Each picks 4–6 end-to-end journeys that cross
many subsystems and traces them completely, e.g.:

- New user signs up via Connect OAuth → tenant resolved → workspace auto-created →
  materializes → asks a question → creates a live artifact → shares the thread →
  a second user with `read` role opens it.
- Two tenants from different providers join one workspace → custom transformation
  applied → materialization re-runs → does every downstream consumer survive?
- A workspace sits idle past TTL → schemas expire → user returns and clicks everything.

Journey tracers are the structural answer to "you need to understand accounts to
review sharing, and materialization to review transformations" — they are explicitly
allowed to read anything and required to follow the flow wherever it goes.

### F. Git historian ×1

Mandate: the arc of change. Regression archaeology (when did contracts drift and which
consumer wasn't migrated), fix-chain analysis, "fixed-where-it-bit" sweeps (a bug class
fixed at one site — list every sibling site).

**Fleet size: ~25–30 reviewers in round 1.** That is the point: three partitions
overlapping means real misses require three independent failures.

## Phase 2 — Adjudication

1. **Dedup/cluster** (1 agent + mechanical keying on subsystem+mechanism): merge
   duplicate findings, recording the replication count of each.
2. **Adversarial verification** — the stage v1 lacked entirely:
   - Every unique finding gets an independent verifier whose mandate is to **refute it**:
     re-trace the code path from entry point to consequence, check reachability,
     check the claimed severity.
   - Verdicts: `CONFIRMED` / `REFUTED` / `PARTIAL` (true but wrong severity/reach),
     always with quoted evidence.
   - S1 / data-loss / security findings get **2 independent verifiers**; disagreement
     spawns a third as tiebreaker.
   - Findings replicated by ≥2 independent reviewers fast-track with one verifier.
3. **Contradiction resolution**: an agent diffs all reports for conflicting claims about
   the same component (v1 example: "data tab complete" vs "data tab partial";
   15 vs 13 writers) and resolves each by reading the code.

## Phase 3 — The gap loop

1. Build the **coverage matrix** from all coverage logs: which files/features/seams were
   deep-read by how many reviewers; which symptom seeds produced zero findings
   (suspicious — either fine or missed).
2. A **completeness critic** gets the matrix + the verified-findings DB + cartography
   and answers: what's cold? which lens found nothing where churn says it should have?
   which journey was never traced? It emits a targeted round-2 roster.
3. Spawn round-2 reviewers (targeted, smaller), re-adjudicate (Phase 2 on the new
   findings only).
4. **Stop when dry**: two consecutive rounds each yielding ≤2 new CONFIRMED findings,
   or the critic certifies the matrix has no cold zones. Track the overlap metric
   between rounds — the v1 observation (60% overlap = keep going) becomes an explicit
   dial.

## Phase 4 — Synthesis

One synthesizer (plus hierarchical pre-synthesis per cluster if the corpus exceeds its
context) produces the canonical report from: the verified-findings DB, cartography, and
the generalists' architecture maps. Required sections (same as v1 prompt, plus):

- Executive summary; as-built architecture map
- Findings: each with verification status, replication count, evidence chain,
  essential-vs-accidental, severity on the **unified scale** (below)
- Cross-cutting patterns; prioritized recommendations with effort + what each unblocks
- What's actually fine
- **Coverage appendix**: what was reviewed at what depth, what wasn't, and how
  confident the report is per area — v1 reports claimed total coverage they didn't have

Then a **red-team pass**: one agent checks the synthesis against the underlying finding
records for drift, overclaiming, or dropped qualifiers.

---

## Shared evidence standards (in every reviewer prompt)

1. **Report only.** No code changes, no PRs.
2. **ACTIVE/S1 claims require the full chain**: entry point → call path → consequence,
   each hop quoted with `file:line`. If you can't quote the chain, label it a hypothesis.
3. **Confidence label per finding**: `verified-by-trace` / `strong-inference` /
   `hypothesis`. (Both v1 reviewers stated misreadings with the same confidence as
   verified facts.)
4. **Comments and docstrings are claims, not facts.** Verify against the logic;
   a comment/code mismatch is itself a finding.
5. **Check reachability**: is the broken thing wired to a route, UI element, or agent
   tool today? Severity depends on it.
6. **Distinguish essential from accidental complexity** explicitly, per finding.
7. **Emit a coverage log**: every file deep-read / skimmed / not opened. Honesty here
   is what makes the gap loop work; "read everything" is not an acceptable entry.
8. **Unified severity scale** — two axes, no per-reviewer dialects:
   - *Status*: `BROKEN-NOW` / `LATENT` / `DEBT` / `COSMETIC`
   - *Impact*: `data-loss` / `security` / `correctness` / `cost-perf` / `velocity`

## Finding record schema

```json
{
  "id": "matlc-003",
  "title": "refresh task loads into old schema then destroys it",
  "claim": "...",
  "chain": [{"file": "apps/workspaces/tasks.py", "line": 172, "role": "entry", "quote": "..."}],
  "status": "BROKEN-NOW", "impact": "data-loss",
  "complexity": "accidental",
  "confidence": "verified-by-trace",
  "reachable_via": "Data Dictionary refresh button",
  "reviewer": "vertical:materialization",
  "replications": ["generalist-1"],
  "verification": {"verdict": "CONFIRMED", "verifiers": 2, "notes": "..."}
}
```

## Mechanics

- **Vehicle**: a Claude Code Workflow (deterministic orchestration, schema-enforced
  structured outputs, resume-on-failure, ~16 concurrent agents with automatic queuing).
  Reviewers write full reports to disk and return structured findings; later phases
  read from disk, so nothing depends on one giant context.
- **Model routing**: default Fable for everything; per-agent override to Opus for the
  security lens if Fable's dual-use guardrails resist (frame all security work as a
  defensive audit of our own production codebase — usually sufficient).
- **Scale estimate**: round 1 ≈ 30 reviewers + ~40–80 verifiers; with the gap loop,
  100–200 agent invocations total. Wall-clock: several hours at the concurrency cap.
  This is the intended spend: the v1 evidence says the marginal reviewer still buys
  new S1-class findings at current overlap levels.
- **Repeatability**: the whole thing re-runs after a quarter of development; the
  coverage matrix and findings DB diff against the previous run, turning this into a
  recurring health check rather than a one-off.

## Open knobs (current defaults in parentheses)

1. Generalist count (3) — raise if round-1 pairwise overlap stays under ~70%.
2. Whether specialists get the symptom seeds (yes) or go in cold like generalists (no —
   correlated steering is acceptable for specialists; independence lives with the generalists).
3. Dry threshold (≤2 new confirmed findings per round, 2 consecutive rounds).
4. Verifier count for non-critical findings (1; 2 for S1/security).
5. Whether Phase 4 also emits a machine-readable backlog (yes — feeds the later
   "change how we build features" conversation).
