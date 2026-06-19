# Cross-Opp Semantic Workspace — Design

**Date:** 2026-06-19
**Status:** Draft for review
**Driving case:** A "KMC Cross-Opp" Scout workspace composing 11 Connect-Labs KMC
opportunities (ids 10012–10022, program 10011) so an operations expert can compare
clinical delivery across opps and trust the comparison.

## 0. Target demo (definition of done)

The demo-driven target — the exact journey that must work end to end:

1. Log in to Scout.
2. Create a new workspace.
3. Add the 11 KMC opps (10012–10022) to it as tenants.
4. Enter one natural-language prompt: *"Build a detailed dashboard on weight gain,
   mortality, and two other interesting indicators, sliced by the dimensions and filters
   that make sense."*
5. Get a **shareable dashboard artifact** with those indicators across opps, with sensible
   slices/filters/dimensions (at minimum `opportunity_id`).
6. **Trust through transparency:** from the artifact, the user can inspect *how each number
   was produced* — the canonical measure definitions, each opp's resolved mapping, and the
   underlying SQL — so they can confirm it is correct, not just take it on faith.

Step 6 is a first-class requirement, not a nice-to-have: the whole point is comparisons the
user can *verify*. "Weight gain" and "mortality" also become the first two canonical measures
the resolver must map across all 11 apps (weight gain = derived from `child_weight_*`
fields; mortality = derived from `child_alive` / discontinuation-reason fields), which is a
concrete test of the semantic resolution.

## 1. Problem

A workspace can already compose multiple tenants (Scout's `WorkspaceViewSchema` /
`ws_<hash>` machinery). What's missing is a way to **ask one question across opps and
get an aligned answer**, even though the underlying CommCare apps differ (271–672
questions each; opp 10012's `stg_visits` is 731 columns).

Path-matching the apps does not work. Measured across all 11 apps:

- 197 form-field paths are identical in all 11, but only ~32 are clinically meaningful
  (the rest are form mechanics: `form_start/end_timestamp_*`, `time_taken_to_fill_*`,
  learn-module `name`/`description`, quiz `question1-5`, `mantra1-10`, GPS calcs).
- The measures that matter most — `child_weight_birth`, `child_weight_visit`,
  `danger_sign_positive`, `high/low_breath_count`, `hypothermia`, `noisy_breathing`,
  `poor_feeding_not_eating`, `gestational_age_at_birth`, `kmc_status`, referral status —
  are present in **10/11**, not 11/11. Strict path-intersection silently drops exactly
  the clinically important fields.

So alignment must be **semantic**, not lexical. This is the whole point of the POC:
the expert speaks in domain terms and the semantic layer (Cube) answers.

## 2. The primitive: workspace-scoped canonical measures

The unit of analysis is the **workspace**. A workspace owns a **canonical measure
catalog** — domain concepts defined once, in plain language, provider-agnostic:

```
birth_weight          — "newborn weight in grams recorded at registration"     (numeric, avg)
danger_sign_detected  — "any clinical danger sign flagged during the visit"    (boolean, rate)
kmc_hours             — "hours of skin-to-skin kangaroo care reported"         (numeric, avg)
visit_approved        — "the visit was approved" (Connect platform field)      (boolean, rate)
```

The expert never sees a CommCare path or a Connect column. They describe **what** they
want. A concept may resolve to a CommCare `stg_visits` expression for one tenant and a
Connect platform-table expression for another — concepts are not tied to a provider.

## 3. Per-tenant resolution (the auto-model)

For each tenant in the workspace, an LLM **resolver** maps every canonical measure to a
concrete SQL expression over that tenant's tables. It is grounded in the app structure
Scout already stores (the labs API `/export/opportunity/<id>/app_structure/` returns the
full HQ application JSON; `_extract_form_definitions` retains a useful slice). Resolver
signals, in priority order:

- **Question `label`** — the primary semantic bridge. The same concept lives at different
  paths across (and within) apps, but the human label is stable: "Stable SVN weight at the
  time of birth (in grams)" → `birth_weight`. Labels are multilingual (`{en, hau}`); use `en`.
- **Form `name` + `module_name`** — role/scoping. Clinical visit measures come from the
  Visit form (module "Visit Management"); `Learn Module` / assessment forms (no `case_type`)
  are excluded so quiz/`mantra` noise never enters a measure.
- **`case_type`** — grain (the KMC case model is `caregiver` / `child` / `visit`).
- **Question `type`** — prefer the real entry question (`Double`/`Int`/`Select`/`Date`)
  over `DataBindOnly` calculated copies, which carry cryptic `#form/...` labels.
- **Choice `options`** — map `Select`/`MSelect` values to concept semantics.
- **Per-tenant column notes / `TableKnowledge`** — accumulated business knowledge.

Output is a stored **mapping** with confidence and provenance:

| canonical measure | opp 10012 | opp 10020 | provenance |
|---|---|---|---|
| `birth_weight` | `child_weight_birth` (Double) | *(absent)* → NULL | label "Birth weight (g)" |
| `danger_sign_detected` | `danger_sign_positive = 'yes'` | `danger_signs <> ''` | label "Danger sign" |

Absence is explicit (NULL + flagged), never silent. Low-confidence mappings are flagged
for review, not hidden.

This is the auto-model: it does the per-app understanding so the human doesn't. It
extends — does not replace — `cube_model_generator`; the difference is it resolves a
**shared workspace catalog** instead of generating each tenant model in isolation.

## 4. Per-opp cubes + a blended cube (Cube Data Blending)

Cube is the object model and composes cubes natively via **Data Blending**: a cube references
another cube's SQL with `${Cube.sql()}`, and a blended cube `UNION ALL`s the referenced cubes
into one surface. We use a **two-tier** model so per-opp knowledge is durable and opps can be
recombined — **Tier 1:** one cube per opp (the reusable unit, generated once from that opp's
resolver, selecting its resolved expressions aliased to canonical names + any opp-specific
measures); **Tier 2:** a blended cube that unions the per-opp cubes via `${opp.sql()}`, stamps
`opportunity_id`, and defines the shared measures **once** (the SELECT bodies below show what
each Tier-1 opp cube resolved to):

```yaml
cubes:
  - name: kmc_cross_opp      # Tier 2 — blended; references Tier-1 cubes opp_10012, opp_10020, ...
    sql: |
      SELECT '10012' AS opportunity_id, visit_id, visit_date, status,
             child_weight_birth             AS birth_weight,
             (danger_sign_positive = 'yes') AS danger_sign_detected
      FROM {opp_10012.sql()} AS a       -- Tier-1 cube reads t_10012_62a6d140.stg_visits
      UNION ALL
      SELECT '10020' AS opportunity_id, visit_id, visit_date, status,
             NULL::float                    AS birth_weight,     -- concept absent in this app
             (danger_signs <> '')           AS danger_sign_detected
      FROM {opp_10020.sql()} AS b       -- different app; its cube emitted NULL birth_weight (absent)
      UNION ALL ...        -- one branch per opp the workspace includes
    dimensions:
      - { name: opportunity_id, sql: opportunity_id, type: string }
      - { name: visit_date,     sql: visit_date,     type: time }
    measures:
      - { name: avg_birth_weight,  sql: birth_weight, type: avg }
      - { name: danger_sign_rate,  sql: "CASE WHEN danger_sign_detected THEN 1.0 ELSE 0 END", type: avg }
```

Because every branch lands on the **same canonical column names**, the union is well-defined
regardless of how different the apps are; an absent concept emits `NULL::<type>`. Per-opp
knowledge stays **durable and reusable**: each Tier-1 opp cube is generated once from that
opp's resolver lineage, and a workspace's blended cube references whatever **subset** of opp
cubes it wants — mix-and-match, no re-derivation. Compilation constraint: `${opp.sql()}`
resolves only when the referenced cubes compile in the **same model context**, so the
cross-opp generator (an extension of `cube_model_generator`) **assembles** the relevant
per-opp cube files + the thin blended cube into `cube/model/ws_<hash>/`. (A rollup /
pre-aggregation is caching, not combination — it may sit *on top of* the blended cube for
speed, never in place of it.)

Consequences — what this design does **not** need:

- **No new Django measure model.** Measures/dimensions live in the Cube model, exactly like
  every other Scout measure today (served via `semantic_catalog` → Cube `/v1/meta`).
- **No separate Postgres union view and no change to `build_view_schema`** for the semantic
  path — the union *is* the cube's `sql`. (A materialized view or a Cube pre-aggregation is
  an optional performance lever at scale; unneeded at demo size, ~5k rows total.)

`semantic_query` / `load_workspace_context` already route a multi-tenant workspace to its
`ws_<hash>` schema and load `cube/model/ws_<hash>/` via `repositoryFactory` — no routing
changes needed.

**Cross-data-source caveat (why this works):** the 11 opps are schemas in **one** Postgres
database, i.e. one Cube data source, so a live `UNION ALL` in a cube's `sql` is valid. Cube
*cannot* union/join across *different* databases in a live query (that requires `rollup_join`
+ Cube Store, with size/partition limits) — not our case, but the reason this approach is
sound only because the tenants share a database.

## 6. Lifecycle (why it is not a one-off)

1. **Onboard / compose** — add the 11 opp tenants to the workspace (existing
   `add_workspace_tenant` API). Materialization builds each tenant's `stg_visits`.
2. **Bootstrap catalog** — auto-propose a starter canonical catalog by mining the
   workspace's tenants (clinical fields common to most), expert prunes. (Reuses the
   `measure_proposer` idea.)
3. **Resolve** — on catalog change or new tenant, the resolver (re)maps every measure per
   tenant; canonical views + union + Cube model are rebuilt. **Measure identity is stable**
   (expert-defined names); only per-tenant expressions under them change.
4. **Review** — the expert sees a coverage matrix (measure × opp: mapped / absent /
   low-confidence) and corrects the few that are wrong.
5. **Persist** — corrections are stored as per-tenant overrides + learnings; future
   resolution and new opps benefit.
6. **New opp (12th) arrives** — step 3 runs automatically; it joins every comparison with
   no human touch beyond reviewing low-confidence mappings.

## 7. Addressing the reviewer bar (Simon / snopoke)

- **Model stability (#303):** measure identity = expert-defined canonical names, never
  LLM-churned. Regeneration only updates per-tenant expressions additively, so artifacts /
  golden queries / saved comparisons keep working. Generated models are persisted + diffed,
  not regenerated-from-scratch.
- **Tenant isolation (#302):** today Cube connects to Postgres as the `platform` superuser
  with no per-request role, and `semantic_query` forwards arbitrary SQL — isolation rests
  entirely on Cube's model surface. This design hardens it: Cube (or the per-workspace
  connection) uses a **least-privilege role granted USAGE only on the workspace's
  constituent schemas**; canonical views reference only `COMPILE_CONTEXT.schema_name`; and
  we ship a **negative test** proving a cross-schema query is *refused*, not just that the
  right one succeeds.
- **Knowledge layering (#305):** the resolver reads per-**tenant** `TableKnowledge` /
  column notes (the data source), not per-workspace — overrides attach to the tenant
  mapping.
- **Ops (#303):** the Django container writes `cube/model/ws_<hash>/`; Cube reads it via a
  shared volume — documented as a deployment requirement.
- **Code quality (#303):** resolver uses LLM structured output (not hand-parsed YAML) and
  validates generated models against Cube's API where feasible.

## 8. New components

No new measure object model — Cube is the catalog. The measures/dimensions live in the
generated cross-opp Cube model; only the resolver and its lineage are net-new.

| Component | Responsibility | Scope |
|---|---|---|
| Resolver service | LLM: canonical measure + tenant app-structure signals → per-opp SQL expression (the one thing Cube can't do) | per tenant |
| Cross-opp cube generator | extend `cube_model_generator` to emit ONE cube whose `sql` is the `UNION ALL` of resolver branches + measures defined once + `opportunity_id` dimension | workspace |
| Resolution lineage | per-opp field / confidence / status / override, stored as per-tenant `TableKnowledge` (Simon #305) — NOT a new measure model | tenant |
| Least-privilege role | Cube reads the workspace's constituent schemas via a role with USAGE on only those schemas | workspace |
| Catalog bootstrapper | auto-propose a starter measure set by mining the tenants (reuses `measure_proposer`) | workspace |
| Coverage + transparency surface | measure × opp coverage; from a figure → Cube measure def (`/v1/meta`) + the cube's union `sql` + per-opp lineage | workspace |

## 9. Scope / phasing

- **Phase 1 (core, this effort):** the resolver (per-opp SQL expression + lineage stored as
  per-tenant `TableKnowledge`); the cross-opp cube generator (one cube whose `sql` is the
  `UNION ALL` of resolver branches, measures defined once, `opportunity_id` dimension) into
  `cube/model/ws_<hash>/`; the least-privilege role + negative isolation test; a hand-seeded
  starter set of ~8–12 KMC measures to prove the path end-to-end on the 11 opps. **No new
  Django measure models; no `build_view_schema` change for the semantic path.**
- **Phase 2:** catalog auto-bootstrap + `measure_proposer` integration; persisted
  overrides + learnings; coverage matrix. Retain the raw `app_structure` JSON (already
  fetched, currently discarded) so the resolver can use case-management bindings
  (question → case property) and the full module tree, not just the extracted slice.
- **Phase 3:** review UI; multi-provider (Connect platform) measures; cross-workspace
  composition.

## 10. Non-goals

- Improving synthetic data realism (connect-labs #670) — pipeline is proven; realism is a
  separate lever.
- Changing the per-tenant `stg_visits` model.
- Replacing `cube_model_generator` — this extends it.

## 11. Testing

- Unit: resolver maps a known measure to the right expression given a form def; absence →
  NULL; override wins over LLM.
- Integration: build canonical union over 2 seeded tenants with divergent field names →
  union aligns; counts correct.
- E2e (cube_e2e): cross-opp `semantic_query` returns per-`opportunity_id` measures across
  the 11 opps; **negative isolation test** — a query for a foreign schema is refused.
