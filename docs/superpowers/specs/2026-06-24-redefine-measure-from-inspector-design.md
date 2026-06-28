# Redefine a measure from the inspector — DDD-driven build

**Date:** 2026-06-24
**Status:** Draft spec (DDD-narrative-driven)
**Narrative slug:** `crossopp-redefine-measure-from-chat`

## 0. What we're building (in one line)

Make Scout's **inspectability writable**: a user who sees a wrong metric definition in the
lineage/Data Dictionary can **redefine it in chat as a derived formula** (`age_days =
visit_date − child_dob` instead of the dead `child_age` field), confirm the new per-opp SQL,
and watch the cube reload and the curve fix itself — **no backend deploy**.

The concrete bug it fixes (real, found 2026-06-24): the resolver maps `age_days` to the literal
`child_age` column, which is decoupled from real age (`corr(weight, child_age) ≈ 0.0`), so the
growth curve is flat — even though the rebuilt synthetic data is good (`corr(weight,
visit_date−dob) ≈ 0.41`, proper catch-up growth). **Good data, wrong metric.**

## 1. How DDD drives this build (read first)

This is **DDD driving a net-new build**, not documenting an existing capability. The difference
is the whole point:

- The **narrative is the acceptance test.** Its scenes' `features[]` are the buildable units.
  The demo opens on the **seeded-broken state** and ends on the **verified-fixed state**.
- The recorder drives the **live product**. On iteration 0 the redefine scenes **fail** —
  the feature doesn't exist yet, the curve stays flat, there's no edit affordance. The dual
  judges flag these as **PRODUCT findings** with `fix_kind: mechanical`.
- **Route findings → build the missing capability → re-render.** Loop: render → judge → build
  → re-render. The loop **cannot converge until the rendered demo actually performs the fix
  and the curve climbs.** A faked demo can't converge — the recorder is hitting real Cube.
- **Convergence == the feature works end to end.** Then `ddd-upload` publishes the package.

So the build sequence is literally the DDD loop:

```
author narrative (= spec)  →  actionability eval (buildable?)  →  narrative-agreement gate
  →  ddd-run (render on the SEEDED-BROKEN workspace)
       iter 0: redefine scenes fail → PRODUCT findings (no derived-measure path, no edit affordance, curve flat)
       build F1..F5 → re-render
       iter N: opens flat → redefines in chat → cube reloads → curve climbs → verified in inspector
  →  both judges pass → converge → upload
```

The narrative below is what we author first; the rest of the spec is the build it implies.

## 2. The narrative (the story arc — broken → fixed)

**Persona:** Maya, KMC clinical operations lead (continuity with the cold-start growth-curve run).

One continuous story, six beats:

1. **The curve is wrong.** Maya opens the infant growth curve for the PIPN workspace. The
   lightest babies' line is flat — it doesn't climb the way catch-up growth should. She doesn't
   trust it.
2. **She inspects how a number was built.** Maya opens the `age_days` lineage and reads, per
   opportunity, that it's resolved to the raw `child_age` field — `CAST(child_age AS NUMERIC)`.
   She recognizes that field isn't real age-since-birth.
3. **She corrects it in plain language.** Maya tells Scout: age should be the **days between the
   visit date and the child's date of birth** — not that field.
4. **Scout re-resolves it as a formula and shows its work.** Scout derives `age_days =
   visit_date − child_dob` **per opportunity** and shows the new SQL for each — including opp
   10020's differently-named DOB column — for Maya to confirm.
5. **She approves and the curve fixes itself.** On approval the cube model reloads (no data
   rebuild); the curve re-queries and now climbs — catch-up growth, lightest band ≥15 g/kg/day.
6. **She verifies the fix where she found the bug.** Maya re-opens the lineage: `age_days` now
   reads `visit_date − child_dob`. She fixed a metric herself, in the product, and can see it's
   right.

`concept_claim`s are falsifiable per beat (e.g. beat 5: "approving a redefined measure reloads
the cube model and the rendered curve changes from flat to climbing without a data rebuild").

## 3. Synthetic / demo setup — seed the WRONG definition

The demo must **open on the broken state**, and elegantly it requires almost no contrivance,
because the wrong definition is the resolver's **natural default output**:

- `setup:` (per-render reseed) materializes the **PIPN pair (10019 + 10021)** with the rebuilt
  (good) data — the data already encodes catch-up growth.
- Build the cross-opp workspace with the **default resolution**, which maps `age_days →
  child_age` (exactly what the resolver does today) → a **flat curve artifact**.
- So scene 1 opens on a genuinely flat curve produced by a genuinely wrong-but-natural metric.
  The bug is **good data + wrong metric definition** — the realistic failure, not an injected one.

This is also why the demo is honest: nothing is faked to make it broken; the resolver's
single-column habit *is* the bug, and the demo is the user fixing it through the product.

Setup outputs the workspace id + the seeded (flat) artifact id as `${...}` vars the scenes use.

## 4. The build the narrative implies (features by scene)

| # | Feature | What to build | Status today |
|---|---------|---------------|--------------|
| F1 | **Derived canonical fields** | `define_crossopp_visit_field` (+ resolver) accepts a *derivation* — operands + a formula ("days between visit_date and child_dob"), resolves each **operand** per opp, and emits `sql_expression = (visit_date::date − child_dob::date)`. Not a single-column match. | **Gap.** Resolver is prompted to "Pick exactly ONE field" (measure_resolver.py:183); `MeasureResolution.sql_expression` exists but only ever carries one cast column. |
| F2 | **Cube builder threads the derived expr** | `_visit_field_select` must emit the derived `sql_expression` (a date-diff → numeric days), not only `_safe_numeric(column)`. | **Mostly there.** It already falls back to `sql_expression` when `column is None`; date-diff casts to numeric. Needs verification + a test. |
| F3 | **Approval card shows the derived per-opp SQL** | The doubt-gate card renders the new `visit_date − child_dob` expression per opp (incl. 10020's column) for confirm/reject. | **Mostly there.** Card already renders per-opp `sql_expression`; confirm the derived form reads well. |
| F4 | **Edit-from-inspector affordance** | An "Edit definition" control on the lineage card / Data Dictionary / `/crossopp` inspector that opens the chat-redefine flow **pre-filled** with the current expression. Turns inspectability from read-only into the correction entry point. | **Gap (new UI + a redefine intent).** |
| F5 | **Agent guidance to offer the fix** | System-prompt nudge: when a per-visit field's confidence is low *or* its curve doesn't track the field, the agent offers to redefine it. | **Gap (prompt).** |

Identity/stability: redefining `age_days` must be **additive** — every other measure's id and
SQL stay byte-stable (#303). The redefine updates exactly one canonical field's expression.

Cube-specific leverage that makes this cheap: a metric **is a definition** in the model, so the
fix is a **model reload, not a re-ETL** — instant, reversible, and localized to each opp's
Tier-1 expression (the Tier-2 `age_week` bucketing and the `avg_visit_weight`/`ci95` measures are
untouched).

## 5. The DDD loop — how it converges

- **iter 0 (render on seeded-broken):** scene 1 flat ✓ (expected), scenes 3–5 fail — no derived
  redefine, no edit affordance, curve stays flat. Judges emit PRODUCT findings
  (`fix_kind: mechanical`): "chat cannot redefine a field to a formula", "no edit-definition
  affordance", "approving does not change the curve". `auto_iterate = continue`.
- **build F1→F4** (the mechanical PRODUCT fixes), re-render only the changed scenes.
- **iter N:** opens flat → Maya redefines → approval shows `visit_date − child_dob` per opp →
  approve → cube reloads → curve climbs → inspector shows the new definition. Both judges pass.
- **converge → `ddd-upload`** the `/ddd/crossopp-redefine-measure-from-chat/<run_id>` package.

The three judged requirements (bake into scenes + judge):
1. The broken state is **genuine** — the flat curve comes from the real default resolution
   (`age_days = child_age`), not an injected fault.
2. The fix happens **entirely through the product** — chat redefine + approval + cube reload; no
   file edit, no deploy, no data rebuild between flat and climbing.
3. The **inspector closes the loop** — the same surface that exposed `age_days = child_age` shows
   `age_days = visit_date − child_dob` after the fix, and the curve is climbing.

## 6. Acceptance

The rendered DDD demo, driving the live product, opens on a flat curve, performs the redefinition
in chat, and ends on a climbing curve (lightest band ≥15 g/kg/day) — with the inspector showing
the corrected definition. Convergence of the DDD run is the end-to-end proof.

## 7. Out of scope (for this run)

- Free-form SQL editing in the approval UI (the user confirms/rejects a derived expression Scout
  proposes; they don't hand-write SQL — preserves the original design's non-goal).
- Making the `child_age` *field itself* internally consistent in the synthetic generator
  (separate, filed against the cloning infra — connect-labs follow-up to #734). This run fixes the
  *metric*, not the field.
- General arbitrary N-operand formula UX beyond what the `age_days` derivation needs (start with
  the date-diff shape; generalize later).
