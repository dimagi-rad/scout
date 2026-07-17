# Scout arch-review remediation plan (2026-06-12)

How to turn the 2026-06-12 architecture review into shipped fixes. Companion to
`synthesis.md` (the canonical report) and `remediation/` (the machine-readable
backlog + issue tooling).

## Operating model (decided)

- **Who executes:** Brian + Claude agents. GitHub Issues are tracking/memory for a
  solo driver, not cross-team coordination. Optimize for agent-driven TDD fixes with
  Brian as the review/design gate.
- **Backlog home:** GitHub Issues, one per remediation *cluster* (~34 — expanding
  §5's ~18 high-priority work-items to give the LATENT/DEBT/COSMETIC tail a home too),
  labeled by wave/tier/impact/effort, plus a tracking meta-issue.
- **Scope:** Comprehensive — Now + Next + Guardrails + the design-gated items.
- **Hotfix posture:** Fix properly, no stop-gaps. The one caveat is `refresh-data-loss`
  (actively destroying prod data on every click): its proper fix is small, so it goes
  **first**, not behind a button-disable.

## What the review already did for us

The synthesis (§5) already clustered 148 raw findings into ~18 numbered work-items in
three tiers with effort sizing. So the answer to "do I need more synthesis to combine
related issues?" is **no big re-clustering** — only a thin traceability map so nothing
is orphaned. That map is `remediation/issue-map.json`, built by
`remediation/build_issue_map.py`, which **hard-fails if any of the 148 findings is
unmapped or double-mapped**. Current state: 148/148 → 34 issues, 0 duplicates.

Two structural facts drive the sequencing:

1. **CI is lying.** CI sets neither `DATABASE_URL` nor `MANAGED_DATABASE_URL`, so the
   real-DB incident-regression suites are silently skipped under a green badge, and
   deploys aren't gated on tests `[12#2, 10#3, 08#4]`. Until that's fixed, *no* fix
   below is actually verified by the suite. → **Wave 0, first.**
2. **"Fixed-where-it-bit" is the #1 predictive pattern.** Incident fixes landed only at
   the site that burned (63-byte guard on view names but not schema/role/dbt names; TTL
   touch in one place not its siblings; connection hygiene in the worker not the MCP
   process; …). The highest-yield action is therefore building a few **guardrails early
   and interleaved** — an identifier helper, a real chat↔MCP contract test, and a
   sibling-sweep PR policy — because they *find and prevent whole classes*, not single
   bugs.

## The 4-track model

- **Track A — Stop-the-bleeding fixes.** BROKEN-NOW data-loss + security, agent-driven,
  TDD, batched by file-locality. (Waves 0–1.)
- **Track B — Structural consolidations.** One permission layer, one status/catalog
  module, credential lifetime, background-work robustness, etc. Some design-gated.
  (Wave 2.)
- **Track C — Guardrails.** CI integrity, contract test, identifier helper, drift
  detection, sibling-sweep policy, quarterly re-run. Built early where they multiply
  force. (Interleaved across waves.)
- **Track D — Genuine product/design decisions.** Not "broken features needing fixes,"
  but "what should this *be*?" These get a `brainstorming → spec → plan` pass *before*
  an agent writes code.

## Design-gated items (Track D)

These four need Brian-in-the-loop design first. They are flagged `design-gated` in the
issue map and must not be handed to an implementation agent cold:

1. **`permission-layer`** — what should READ / READ_WRITE / MANAGE actually permit on
   content? Should recipe `is_shared`/`is_public` exist at all, or be removed?
2. **`status-catalog-module`** — define the single canonical shape for materialization/
   schema status and the table catalog, then implement once.
3. **`multitenant-retrofit-shim`** — what *should* a multi-tenant (A+B+C) workspace show
   in Data Dictionary / artifacts / knowledge? The first-tenant shim is a stand-in for
   an unmade product decision.
4. **`content-satellite-redesign`** *(epic)* — recipes + knowledge + artifacts. The
   wave-1/2 fixes restore these to working-**as-designed**; whether that design is what
   we want is separate. Brian's call: repair-in-place vs rethink. The fixes don't waste
   redesign effort — you need the features working to even evaluate a redesign.

Everything else is a *fix*, not a redesign — including recipes, which is shipped behind
a working UI and broken by a March signature change, not unbuilt.

## Sequencing (waves)

Full per-issue detail with finding checklists: `remediation/backlog.md`.

- **Wave 0 — make the safety net real.** `ci-deploy-integrity`,
  `chat-mcp-contract-test`. Small, unblocks trust in every later PR.
- **Wave 1 — stop active harm (BROKEN-NOW data-loss + security).**
  - *Schema-lifecycle group* (one worktree, sequenced — these all pile into
    `tasks.py` / `schema_manager.py` / `materializer.py`): `refresh-data-loss` **first**,
    then `identifier-minting-helper` + `mcp-teardown-and-state-cas`.
  - *Independent leaves (parallelizable across worktrees — distinct files):*
    `recipe-runner-fix`, `artifact-sandbox-isolation`, `artifact-multitenant-render`,
    `dbt-transformations`, `onboarding-apikey-404`, `high-blast-one-liners`,
    `mcp-metadata-disclosure`, `ocs-team-scope`, `frontend-tool-cards`,
    `frontend-workspace-switch`, `base-path-and-labs`.
- **Wave 2 — structural consolidations.** Design-gated items get their brainstorm first:
  `permission-layer`, `status-catalog-module`, `multitenant-retrofit-shim`,
  `content-satellite-redesign`. Then: `credential-lifetime-long-jobs`, `mcp-hardening`,
  `cost-latency-floor`, `background-work-robustness`, `truthful-failure`,
  `observability`, `auth-perimeter-hardening`, `account-merge-correctness`,
  `admin-lockdown`, `infra-network-security`, `knowledge-fixes`.
- **Wave 3 — tail, remaining guardrails, cleanup.** `provider-data-quality`,
  `reference-drift-detection`, `checkpoint-retention-privacy`, `dead-code-cleanup`,
  `rerun-review-quarterly`.

### Conflict-grouping rule

Do **not** spin up one parallel worktree per finding. The hot files
(`tasks.py`, `schema_manager.py`, `materializer.py`, read by 13–14 reviewers each) are
touched by most high-priority schema fixes — naive parallelism = merge hell, plus there
are real ordering deps (the identifier helper must exist before the collision fixes
adopt it). **Group by file-locality into one worktree → one PR; parallelize only the
genuinely disjoint leaves** (frontend cards, onboarding 404, the one-liners).

## Per-fix agent mechanic

For each agent-suitable issue, follow the established PR workflow:

1. Isolated worktree (use **relative paths** after entering; copy the gitignored `.env`
   in — the absolute-path gotcha).
2. TDD: the issue body carries each finding's `claim` + entry→consequence `chain` +
   exact `files` — a ready-made repro spec. Step 1 is *write the failing test that
   reproduces the chain*, then fix, then verify (`uv run pytest`, the now-real CI suite).
3. **Sibling sweep** (policy): grep for sibling sites of the bug class and fix or
   explicitly tick them off in the PR (Pattern 1 is the highest-yield finding generator).
4. Converge file-local fixes into one integration worktree → **one PR per conflict
   group**, iterate against the auto-review until clean, then add **snopoke** as reviewer.

Design-gated issues stop at step 0: `brainstorming → spec → writing-plans` first.

## Artifacts in `remediation/`

| File | What it is |
|---|---|
| `build_issue_map.py` | Encodes the finding→issue clusters; **asserts 148/148 coverage**; regenerates the two outputs below. |
| `issue-map.json` | Self-contained map (per-finding claim/chain/files inlined) consumed by the issue creator. |
| `backlog.md` | Human-readable checklist grouped by wave. Regenerate, don't hand-edit. |
| `create_issues.py` | Creates GH labels + one issue per cluster + a tracking meta-issue. **Dry-run by default**; `--execute` to commit; `--only <keys>` to scope. |

### Running it

```bash
cd docs/arch-review/2026-06-12/remediation
uv run build_issue_map.py          # regenerate map + backlog (coverage check)
uv run create_issues.py            # DRY RUN — prints what it would create
uv run create_issues.py --execute  # really create labels + issues + meta-issue
```

## Open gates (do not skip)

1. Brian reviews `issue-map.json` / `backlog.md` — is the clustering right? — **before**
   any issue is created.
2. Only after that: `create_issues.py --execute`.
3. Then `writing-plans` for Wave 0 + `refresh-data-loss`, and execute via the worktree
   flow.
4. Design-gated items get their own `brainstorming` session each before implementation.
