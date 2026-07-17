# Gap round 1 — Django admin & operator commands as unreviewed mutation paths

*Reviewer: gap1-admin-privileged-write-paths. Date: 2026-06-12. Repo @ `35e4230`.*

Scope: all six `apps/*/admin.py` files (read in full), the staff/superuser model and
production admin exposure, `backfill_readonly_roles.py` (read in full), the other three
management commands, and a sweep of every `RunPython`/`RunSQL` data migration.

Per the shared evidence standards: every finding carries a status/impact/confidence
label; chains are quoted `file:line`; known findings from the round-1 fleet are not
re-reported (admin/command-specific *new sites* of known classes are).

---

## Context: what the admin surface actually is

- `/admin/` is mounted unconditionally — `config/urls.py:82` `path("admin/", admin.site.urls)` —
  and nothing in `config/settings/production.py` restricts it (the file contains only
  cookie/HSTS/logging config; no admin path randomization, no IP allowlist, no 2FA, no
  rate limiting). The production API container serves it to the internet.
- Registered admins (verified by grepping every `@admin.register` / `admin.site.register`):
  `User`; `TenantSchema`, `MaterializationRun`; `TableKnowledge`, `KnowledgeEntry`,
  `AgentLearning`; `Recipe`, `RecipeStep`, `RecipeRun`; `Artifact`; `TransformationAsset`,
  `TransformationRun`, `TransformationAssetRun`. Plus third-party: allauth's
  `SocialApp`/`SocialAccount`/`SocialToken` admins and procrastinate's read-only
  `ProcrastinateJob` admin.
- NOT registered: `Workspace`, `WorkspaceTenant`, `WorkspaceMembership`,
  `WorkspaceViewSchema`, `TenantMetadata`, `Tenant`, `TenantMembership`,
  `TenantConnection`, `Thread`, `ThreadJob`.
- Who gets `is_staff`: only `createsuperuser` (`apps/users/models.py:41-42` defaults
  `is_staff=True, is_superuser=True`) and the merge OR-propagation
  (`apps/users/services/merge.py:235-240`). There is no operator allowlist, no SSO/OAuth
  gate for admin, and no code path that grants plain staff.

---

## Findings

### F1. TenantSchema/MaterializationRun admin exposes raw state-machine fields with zero hooks; an admin Save can re-arm the TTL janitor into `DROP SCHEMA CASCADE`

**Status: LATENT · Impact: data-loss · Confidence: verified-by-trace · Complexity: accidental**

`apps/workspaces/admin.py` is 21 lines of default ModelAdmin:

- `TenantSchemaAdmin` (`apps/workspaces/admin.py:10-14`): `readonly_fields = ["id", "created_at"]`
  only. Therefore `state`, `schema_name`, `tenant`, and `last_accessed_at` are all
  editable form fields (model fields at `apps/workspaces/models.py:28-40`).
- `MaterializationRunAdmin` (`apps/workspaces/admin.py:17-21`): `readonly_fields = ["id", "started_at"]`
  only — `state`, `result`, `progress`, `procrastinate_job_id`, `completed_at` all editable.
- `TransformationRunAdmin` / `TransformationAssetRunAdmin` (`apps/transformations/admin.py:20-32`)
  have the same shape (`status` editable).

Every state transition in production code is CAS-guarded (`filter(state=...).update(...)`)
or janitor-reconciled; the admin is the one writer that does a **full-row form save** with
none of those invariants. Concrete consequences, each traceable:

1. **Re-armed teardown.** Operator flips an `EXPIRED` TenantSchema back to `ACTIVE`
   (e.g. "the schema is physically still there, let me reactivate it") without touching
   `last_accessed_at`. Chain: admin save → row is `state=ACTIVE, last_accessed_at < cutoff`
   → TTL janitor `expire_inactive_schemas` (`apps/workspaces/tasks.py:538-544`) selects
   exactly `state=SchemaState.ACTIVE, last_accessed_at__lt=cutoff`, sets `TEARDOWN`, defers
   `teardown_schema` → `DROP SCHEMA CASCADE`. This is the operator-keyboard edition of the
   2026-06-10 incident-b class (provision resurrecting EXPIRED rows without touch), and the
   admin is the easiest place to do it.
2. **Lost-update clobber of concurrent transitions.** Django ModelAdmin saves all form
   fields. An admin page opened while a materialization is running and saved minutes later
   writes back the stale `state`/`last_accessed_at` it loaded, silently reverting any
   transition (janitor, CAS, `touch()`) that happened in between.
3. **`schema_name` editable** on a row with `unique=True` but no sync to the physical
   schema or the derived `{schema_name}_ro` role (`schema_manager.py:33-35`) — one edit
   desyncs Django state, the physical schema, and the role name that `SET ROLE` will use.
4. **Delete is live and unhooked.** Default admin delete on TenantSchema cascade-deletes
   `MaterializationRun` rows (`models.py:84-88` FK CASCADE) and leaves the physical schema
   and `_ro` role orphaned — no `manager.teardown()` hook. The dependency-graph finding
   from round 1 counted four live mutation paths lacking hooks; the admin adds list-page
   delete, change-form save, and bulk delete as three more, all reachable today by any
   superuser.

Contrast: procrastinate's bundled admin does this correctly — every concrete field
readonly and `has_change/add/delete_permission → False`
(`site-packages/procrastinate/contrib/django/admin.py:68-86`). Scout's own admins for its
two most dangerous state machines have no such guard, and the two state-machine models
that janitors most often need inspected (`WorkspaceViewSchema`, `ThreadJob`) are not
registered at all (see F7).

**Reachable via**: `/admin/workspaces/tenantschema/` and `/admin/workspaces/materializationrun/`
for any superuser (or staff granted those model perms) — live in production.

### F2. Production `/admin/login/` is an unthrottled password brute-force surface that bypasses both allauth and Scout's own auth rate limiter

**Status: LATENT · Impact: security · Confidence: verified-by-trace · Complexity: accidental**

- Scout's API login/signup have a per-email lockout: `apps/users/auth_views.py:109` and
  `:150` call `check_rate_limit(email)` (5 attempts / 300s, `apps/users/rate_limiting.py:5-11`).
- The admin login at `/admin/login/` (mounted via `config/urls.py:82`) uses Django's
  stock `AdminSite.login` → `authenticate()` → `ModelBackend`
  (`config/settings/base.py:183-186`), which has **no** rate limit, no lockout, no 2FA.
  allauth's rate limits live in allauth views, not in its backend, so they don't apply
  either. `production.py` adds nothing.
- Even the API-side limiter is per-process `LocMemCache` (`config/settings/base.py:318-326`,
  with an acknowledging NOTE comment) — the known chat-rate-limit finding generalizes to
  the auth limiter — but the admin path has *zero*, not merely per-process, protection.
- Target accounts exist: `createsuperuser` is the documented setup path (`README.md:96`,
  `inv createsuperuser`) and created the known prod dev-artifact superuser. Superusers
  hold every model permission including `SocialToken` (F3) and the F1 state knobs.
- Secondary footgun inside the same surface: `UserAdmin` exposes
  `is_staff`/`is_superuser`/`user_permissions` as editable fields
  (`apps/users/admin.py:23-26`), so any staff user holding `users.change_user` can
  self-escalate to superuser — standard Django behavior, but in a system whose only
  intended identity model is OAuth/allauth, the admin's parallel password-identity world
  is entirely outside that model (no email verification, no provider linkage, no audit).

**Reachable via**: `https://<prod-host>/admin/login/`, unauthenticated, today.

### F3. Admin exposes every user's OAuth access/refresh tokens and the OAuth client secrets in plaintext (allauth `SocialToken`/`SocialApp` admin)

**Status: DEBT · Impact: security · Confidence: verified-by-trace · Complexity: accidental**

`allauth.socialaccount` in `INSTALLED_APPS` (`config/settings/base.py:65`) auto-registers
`SocialTokenAdmin` and `SocialAppAdmin`
(`site-packages/allauth/socialaccount/admin.py:50,68-69`). Nothing in Scout unregisters
or restricts them. A superuser — or the attacker who brute-forces one via F2 — gets a
browsable, searchable list of every user's CommCare/Connect/OCS access+refresh tokens
(`SocialToken.token`/`token_secret`) and the platform's OAuth client secrets. Scout went
to the trouble of Fernet-encrypting project DB credentials (`DB_CREDENTIAL_KEY`), but the
tokens that resolve to those tenants' data sit in plaintext behind the weakest login on
the box. This is also the only place in the stack where refresh tokens are *displayed*
rather than used.

**Reachable via**: `/admin/socialaccount/socialtoken/` for any superuser — live in production.

### F4. `backfill_readonly_roles` aborts on the first Django-vs-physical drifted schema, leaving later schemas role-less (fail-closed query outage); no per-schema error handling or `--dry-run`

**Status: LATENT · Impact: correctness · Confidence: verified-by-trace · Complexity: accidental**

The command (`apps/workspaces/management/commands/backfill_readonly_roles.py`, read in
full) is the operational backstop for the SET ROLE readonly defense. What's right:

- **Idempotency claim is true**: `_create_readonly_role` checks `pg_roles` and suppresses
  `DuplicateObject` (`schema_manager.py:595-608`); all GRANTs are idempotent;
  the connection is `autocommit=True` (`schema_manager.py:43`), so completed grants persist.
- **Consumer fails closed**: if a role is missing, `SET ROLE` at
  `mcp_server/services/query.py:44` raises — queries error rather than running as the
  privileged connection user. (Note in passing: `_execute_async_parameterized` at
  `query.py:68-80` never does `SET ROLE` at all — round-1 known finding territory.)

What's wrong:

1. **One bad row kills the rest of the run.** The loop bodies
   (`backfill_readonly_roles.py:34-36, 43-63`) have no try/except. Any `ACTIVE`
   TenantSchema row whose physical schema is gone — and round 1 verified at least three
   producers of exactly that drift (MCP `teardown_schema`, `purge_synced_data`, manual
   psql) — makes `GRANT USAGE ON SCHEMA` raise `InvalidSchemaName`, aborting the command.
   Every schema after it in iteration order never gets its role, and all *their* MCP
   queries then fail at `SET ROLE` until someone fixes the drifted row and re-runs.
   For a backstop command whose whole purpose is post-incident repair, "crashes on the
   first symptom of the incident class it exists to repair" is the wrong failure mode.
2. **Selection drift**: it includes `SchemaState.MATERIALIZING`
   (`backfill_readonly_roles.py:31-33`), a state round 1 verified is never written by
   production code, and excludes `PROVISIONING` — harmless today but it encodes the dead
   state machine rather than the real one.
3. **Point-in-time view grants**: the view-schema branch grants `SELECT ON ALL TABLES`
   in constituent tenant schemas to the view role (`:57-62`) but sets no
   `ALTER DEFAULT PRIVILEGES` for the *view* role there — mirroring the build path
   (`schema_manager.py:392-405`), so correctness depends entirely on the round-1-flagged
   hand-maintained rebuild hooks re-running grants after every rematerialization. The
   backfill cannot repair a workspace mid-drift between materialization and rebuild.
4. No `--dry-run`, no count summary, no logging (only stdout writes) — contrast
   `purge_synced_data`, which does this properly.

**Reachable via**: operator shell (`manage.py backfill_readonly_roles`); the failure mode
needs pre-existing Django/physical drift, hence LATENT.

### F5. `merge_users` OR-propagates `is_staff`/`is_superuser` from the duplicate onto the canonical account

**Status: DEBT · Impact: security · Confidence: verified-by-trace · Complexity: accidental**

`apps/users/services/merge.py:235-240`: if the *duplicate* (usually the stale, about-to-be-
deleted row — e.g. a `createsuperuser` dev artifact) has `is_staff`/`is_superuser`, the
canonical user silently inherits them. The merge command's printed plan
(`merge_duplicate_users.py:96-107`) summarizes repoint counts but does not surface the
privilege change at confirmation time (it lives only inside the MergeReport changes dict).
Operationally this means "clean up the duplicate dev account" can promote a normal OAuth
user to superuser of the production admin (F1-F3 capabilities) as a side effect. Given the
2026-06 prod merge of exactly such an artifact, this is plausibly already the state of
production. Deliberate code, but the wrong default direction for a privilege bit during a
cleanup operation, and invisible at the confirmation prompt.

**Reachable via**: `manage.py merge_duplicate_users` (operator shell).

### F6. `setup_oauth_apps` composes wrong env-var names for Google/GitHub (`GOOGLE_OAUTH_OAUTH_CLIENT_ID`) and its skip message + docstring name two further different spellings

**Status: LATENT · Impact: velocity · Confidence: verified-by-trace · Complexity: accidental**

`apps/users/management/commands/setup_oauth_apps.py:16-22` sets
`env_prefix="GOOGLE_OAUTH"` / `"GITHUB_OAUTH"`, and `:46-47` reads
`f"{env_prefix}_OAUTH_CLIENT_ID"` → the command actually reads
`GOOGLE_OAUTH_OAUTH_CLIENT_ID` / `GITHUB_OAUTH_OAUTH_CLIENT_ID`. The skip message (`:50`)
prints `f"{env_prefix}_CLIENT_ID not set"` → `GOOGLE_OAUTH_CLIENT_ID`, and the module
docstring (`:3-4`) promises `GOOGLE_OAUTH_*` and `COMMCARE_CONNECT_OAUTH_*` (actual:
`CONNECT_OAUTH_*`, matching `config/deploy.yml:49-50`). An operator following either the
docstring or the error message can never bootstrap Google/GitHub via this command; it
silently "skip"s forever. Unused in the current prod deploy (deploy.yml passes only
COMMCARE/CONNECT/OCS vars), and recoverable by hand-creating the SocialApp in admin —
hence LATENT/velocity, not BROKEN-NOW. The CONNECT docstring drift is the same
comment-vs-code class round 1 found elsewhere.

### F7. Admin registration is inverted: the dangerous raw rows are fully editable while every model an operator actually needs is absent

**Status: DEBT · Impact: velocity · Confidence: verified-by-trace · Complexity: accidental**

Unregistered: `Workspace`, `WorkspaceTenant`, `WorkspaceMembership` (roles!),
`WorkspaceViewSchema` (a state machine central to the 2026-06-10 incidents), `ThreadJob`
(the state machine behind the 19-commit resume fix chain), `Thread`, `Tenant`,
`TenantMembership`, `TenantConnection`, `TenantMetadata`. Every recent incident
(zombie jobs, stuck `Preparing…`, EXPIRED-row resurrection, cascade-FAILED view schemas)
required inspecting or repairing exactly these rows, and the only tools were psql or
bespoke commands — while `TenantSchema.state` sits fully editable (F1). The admin
surface as shipped maximizes the harm an operator can do and minimizes the help it
offers. A read-only registration of the missing state machines (procrastinate's admin as
the template) plus locked-down versions of F1's would invert both.

### F8. RecipeAdmin manages a vestigial model shape: the live `Recipe.prompt` field is absent from the admin while dead `RecipeStep` machinery is its centerpiece

**Status: DEBT · Impact: velocity · Confidence: verified-by-trace · Complexity: accidental**

The runner executes `Recipe.prompt` via `render_prompt`
(`apps/recipes/models.py:53`, `services/runner.py:195`). `RecipeAdmin`'s fieldsets
(`apps/recipes/admin.py:39-63`) omit `prompt` entirely — the one operator surface for
recipes cannot view or edit the only field that executes. Instead the admin is built
around `RecipeStep` (inline `:11-17`, dedicated `RecipeStepAdmin` `:74-90`,
`step_count` `:65-67`), which neither `runner.py` nor `api/views.py` reference (grep:
zero hits) — round 1's dead-code cluster named `RecipeStep`; the admin angle is new:
it actively invites operators to create rows nothing reads, and `RecipeRunAdmin.step_progress`
(`:166-171`) computes progress against the dead model, so every run shows `N/0`.
Similarly the soft-delete fields aren't surfaced and the default manager hides deleted
recipes/artifacts (`apps/recipes/models.py:15-17`, `apps/artifacts/models.py:16-18,132`),
so admin can't restore them either.

### F9. Data migrations 0003/0004 (the two never-reviewed `RunPython`s) are mostly safe; 0004's dedup can cascade-delete `TenantMetadata` hanging off the discarded duplicate membership

**Status: DEBT · Impact: correctness · Confidence: strong-inference · Complexity: accidental**

Full sweep result: the codebase contains exactly three `RunPython` migrations (users
0003, 0004, 0007) and zero `RunSQL`; 0006/0007 were previously reviewed. 0003
(`empty emails → NULL`) is trivially safe and idempotent. 0004
(`deduplicate_tenant_memberships`) keeps the earliest-created membership and deletes the
rest. `TenantMetadata.tenant_membership` is a CASCADE OneToOne
(`apps/workspaces/models.py:266-270`) that predates the migration (workspaces 0001,
2026-03-17 vs users 0004, 2026-04-22), so any provider-discovery metadata attached to a
*later* duplicate membership was deleted while the kept membership may have had none —
the same metadata-loss class as the known merge-conflict finding, at an earlier site.
One-time, already executed in prod; relevant now only as a pattern (both dedup sites
prefer "earliest row" over "row with data"). Reverse migrations are noops, which is
correct for both.

---

## What's fine

- **`backfill_readonly_roles` idempotency**: the help-text claim is verified true —
  `pg_roles` pre-check + `DuplicateObject` suppression + idempotent GRANTs + autocommit.
- **Missing readonly role fails closed**: `SET ROLE` errors rather than silently running
  privileged (`mcp_server/services/query.py:44`) — backfill gaps cause outage, not exposure.
- **Knowledge admin write hygiene**: `TableKnowledgeAdmin.save_model` stamps `updated_by`,
  `KnowledgeEntryAdmin` preserves original `created_by` (`apps/knowledge/admin.py:37-39,54-57`);
  timestamps readonly; autocomplete targets have the required `search_fields`.
- **`RecipeRunAdmin` and `share_token` fields**: run execution fields and all share tokens
  are readonly in every admin that shows them (`recipes/admin.py:35,108-119`) — admin
  cannot mint or alter tokens. (`is_shared`/`is_public` editable, but their unenforcement
  is a known round-1 finding.)
- **`AgentLearningAdmin` actions**: approve/reject/±confidence actions use the model's
  clamped helpers or bounded arithmetic (`knowledge/admin.py:146-175`,
  `knowledge/models.py:192-198`); they're the only working lifecycle the Learning system
  has. (Cosmetic: `confidence_badge` relies on `allow_tags` — removed in Django 2.0 — so
  the list column renders escaped literal `<span …>` HTML, `knowledge/admin.py:142-144`.)
- **`purge_synced_data` command ergonomics**: dry-run by default, `--confirm` gate,
  per-schema try/except with an explicit "records deleted anyway" warning — the error-handling
  model F4 should copy. (Its orphaning of view schemas is a known round-1 finding.)
- **`merge_duplicate_users` shell**: dry-run plan, confirmation prompt, per-merge
  exception isolation, `--canonical-id` override validated against the group.
- **procrastinate's bundled job admin**: fully read-only with all permissions denied —
  third-party code already demonstrates the correct pattern for F1/F7.
- **0003 and the 0007 reverse**: safe, idempotent, reversible-as-noop where appropriate.

## Coverage log

**Deep (line-by-line):** all six admin.py files (`apps/{knowledge,recipes,artifacts,workspaces,users,transformations}/admin.py`);
`apps/workspaces/management/commands/backfill_readonly_roles.py`;
`apps/workspaces/management/commands/purge_synced_data.py`;
`apps/users/management/commands/merge_duplicate_users.py`;
`apps/users/management/commands/setup_oauth_apps.py`;
`config/settings/production.py`; `config/urls.py`;
`apps/users/migrations/0003_*.py`, `0004_*.py`; `apps/users/rate_limiting.py`;
`schema_manager.py` lines 25-60, 360-410, 520-650; `tasks.py` lines 518-554;
`mcp_server/services/query.py` lines 20-80; model definitions for TenantSchema,
MaterializationRun, WorkspaceTenant, TenantMetadata, Recipe/RecipeStep/RecipeRun (field
lists), Artifact (managers/soft-delete); procrastinate + allauth vendored admin.py.

**Skimmed:** `config/settings/base.py` (INSTALLED_APPS, backends, allauth flags, CACHES);
`apps/users/auth_views.py` (login/signup rate-limit call sites only); `apps/users/models.py`
(create_superuser, membership related_names); `apps/recipes/services/runner.py` (prompt
consumption only); `merge.py` (lines 235-240 only); `deploy.yml`/`DEPLOYMENT.md` (OAUTH
env names only); cartography.md (first 100 lines).

**Not examined:** admin *templates*/static overrides (none suspected, not verified);
allauth `/accounts/` URL surface mounted at `config/urls.py:83` (what stock allauth views
are live in prod beyond admin — flagged for another reviewer); whether nginx/CloudFront in
`infra/scout-stack.yml` adds any /admin/ restriction (grepped deploy.yml only);
`apps/users/migrations/0005`, `0006` bodies (declared reviewed/schema-only — trusted);
all non-users migrations' non-RunPython operations (swept for RunPython/RunSQL only);
Django model-permission fixtures/groups in any seed data (none found by grep, not
exhaustively confirmed); `merge.py` beyond the privilege-flag block; the full
`infra/` stack definition; runtime state of prod (which staff users actually exist).
