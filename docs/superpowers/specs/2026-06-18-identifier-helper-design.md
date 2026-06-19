# One identifier helper — 63-byte + collision guard (#235)

_2026-06-18 · branch `arch/235-identifier-helper` · Closes #235_

## Problem

PR #227 fixed Postgres-identifier length/collision guards for **view names only**.
The sibling sites that mint every other name are unguarded, so the cross-tenant
collision class is still open (same family as the 2026-06-10 incident):

- **00#3** — `Tenant` is unique on `(provider, external_id)`, but `provision()`
  resolves an existing `TenantSchema` by `schema_name` **alone**, and
  `_sanitize_schema_name` maps distinct external_ids to one name
  (Connect `123` & OCS `123` → `t_123`; `a-b`/`a_b`/`a.b` collide). A tenant can
  be routed into another tenant's live ACTIVE schema (cross-tenant destruction +
  disclosure). `load_tenant_context` filters `tenant__external_id` with **no
  provider predicate**; `build_view_schema` does `Tenant.objects.get(external_id=)`
  → `MultipleObjectsReturned` breaks multi-tenant builds.
- **00#4** — schema / `_ro` role / `_dbt` role / `_r{hex}` refresh names have **no
  63-byte guard**. Postgres silently truncates to 63 bytes → distinct Django rows,
  one physical schema; `_ro` and refresh suffixes truncate *differently*, so a
  shared truncated `_ro` role becomes a cross-tenant read primitive. Live exposure
  = free-text CommCare domains.
- **04#6** — dbt model names + column aliases from CommCare metadata have no
  63-byte guard; `_unique_alias` disambiguates **before** truncation, so long
  names still collide in the physical relation. (Secondary, **out of scope**: the
  synchronous `/runs/trigger/` per-process `threading.Lock` — note only.)

## Design

### New module `apps/common/identifiers.py` — the single chokepoint

`mcp_server` already imports `apps.*`, so this is reachable from every minting site.

```python
fit_identifier(base, *, suffix="", unique_key=None, max_bytes=63) -> str
```
Sanitize `base` (lowercase; `-`/`.`/space → `_`; strip non-alnum; ensure a leading
letter), append `suffix`, cap to `max_bytes` (byte length, UTF-8). When
`unique_key` is given, weave a deterministic 8-char `sha256(unique_key)` digest in
so distinct keys never collide — even when sanitized bases match or the name
truncates. **The hash and suffix are always preserved; only the readable head is
trimmed to fit.**

Public minting functions, all built on `fit_identifier`:

| Function | Keyed on | Behaviour |
|---|---|---|
| `tenant_schema_name(provider, external_id)` | `(provider, external_id)` | **always** carries the digest → Connect `123` ≠ OCS `123`. Capped ~50 bytes so suffixes still fit. |
| `refresh_schema_name(provider, external_id)` | identity + random token | per-refresh-unique, ≤63. |
| `readonly_role_name(schema_name)` | `schema_name` | returns plain `{schema}_ro` when it fits ≤63 (**byte-identical to today**); digest-fit only on overflow. |
| `dbt_role_name(schema_name)` | `schema_name` | same treatment for `_dbt`. |
| `dbt_model_name(base)` | full pre-truncation name | cap+hash **before** truncation. |
| `dbt_column_alias(base, seen)` | full pre-truncation name | unique-disambiguate **then** cap+hash. |
| view prefix / view name | reuse `fit_identifier` | dedups #227's bespoke truncation; hash keyed on `(provider, external_id)`. |

### Correctness fixes (00#3)

- `provision()` matches an existing schema by **`tenant` FK** (not `schema_name`).
  Removes cross-tenant sharing **and** preserves existing physical schemas verbatim.
- `load_tenant_context(external_id, provider)` — `provider` becomes a **required
  predicate**, threaded from its two callers (`load_workspace_context` and
  `apps/agents/graph/base.py`), both of which already hold the `Tenant`.
- `build_view_schema` threads the **tenant object** through instead of
  re-`get(external_id=)` → kills the `MultipleObjectsReturned` break.

### Migration: none

Existing schemas are **left untouched**. `provision()` FK-matches and reuses the
stored `schema_name`, and the derived-name functions are byte-identical for the
short names that exist in prod, so queries keep working with no interruption. The
inactivity TTL expires old-shape schemas; the next access mints the new
collision-safe shape — within ~a week prod is all new-shape names. The only
non-seamless case is an existing >60-byte schema name (the latent-bug zone): its
role name changes → queries **fail closed** until TTL regen, which is the correct
secure outcome (better than a shared truncated `_ro`) and self-heals.

## Testing (real-DB, `@pytest.mark.django_db(transaction=True)` where DDL runs)

1. **Cross-provider same external_id → distinct schemas** — Connect `123` & OCS
   `123` provision to two different physical schemas; neither sees the other's data.
2. **Punctuation collision** — `a-b` / `a_b` / `a.b` (same provider, distinct
   tenants) → distinct schemas.
3. **>63-byte external_id → distinct, ≤63-byte, hash-suffixed schemas** — two long
   ids sharing a 63-byte prefix get distinct physical schemas.
4. **Derived names ≤63 bytes & injective** — `_ro` / `_dbt` / `_r{hex}` of a
   max-length schema all stay ≤63 and distinct per schema; short names stay
   byte-identical (`t_123_ro`).
5. **`provision()` matches by tenant FK** — second tenant computing a colliding
   sanitized base does **not** receive the first tenant's schema.
6. **`load_tenant_context` provider predicate** — two tenants sharing external_id
   across providers resolve to their own schema.
7. **dbt model/alias 63-byte guard** — long form/case names and long property
   paths produce distinct ≤63-byte relations/aliases.
8. **Sibling-sweep grep** — a test (or PR-body grep) shows no raw schema/role/view/
   dbt name minting bypasses the helper.

## Out of scope

The async-trigger redesign for `TransformationRunViewSet.trigger` (the per-process
`threading.Lock`). Left with a `# note` only.
