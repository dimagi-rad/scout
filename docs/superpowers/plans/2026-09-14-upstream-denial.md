# Upstream denial implementation plan

**Goal:** Resolve #378/#384 by making authoritative upstream denials revoke exactly the affected access, while retaining access on inconclusive failures.

**Architecture:** A shared transactional denial recorder owns membership archival and durable connection denial state. Discovery and materialization pass the exact identity/credential used; stale observations cannot revoke a replaced credential. A successful complete tenant discovery restores confirmed memberships and clears connection denial. All-of enforcement, periodic refresh, legacy scope cleanup and UI coverage prompts remain subsequent PRs.

**Approved direction:** The user approved sequential access PRs, starting with durable upstream denial handling. No additional product choice is needed for this slice.

## Chunk 1: Scope and evidence

- [x] Read #378/#384 and current main; isolate branch from #438/#439 work.
- [x] Find discovery, OAuth refresh and materializer exception boundaries.
- [x] Baseline existing discovery tests on a dedicated local PostgreSQL test database.
- [x] Add failing tests for connection-wide 401, tenant-only 403, unrelated users/teams/connections, stale credential observations, restoration and transient failure retention.

## Chunk 2: Shared recorder and discovery

- [x] Add durable denial code/time fields to TenantConnection and migration.
- [x] Add `apps/users/services/upstream_denial.py`: sync atomic recorder plus async wrapper; serialize with existing user lock and verify exact connection/credential observation before writing. Only allow authoritative auth codes. Tenant 403 must not mark the whole credential dead.
- [x] In `tenant_resolution.py`, record discovery 401/403 before raising existing non-expected provider exceptions. Discovery 403 is scoped to the enumerated identity/connection, not an arbitrary tenant. Preserve identity binding and successful restoration behavior.
- [x] In `token_refresh.py`, only invalid_grant is an authoritative credential revocation; invalid_client, timeouts, 429 and 5xx remain non-revoking failures. Preserve typed distinction in sync and async paths.

## Chunk 3: Loader integration

- [x] Preserve inconclusive mid-run refresh errors instead of converting them into final 401.
- [x] Observe the actual token after successful mid-run rotation.
- [x] In `materializer.py`, record typed denials at the common DISCOVER/LOAD exception boundary; preserve run failure accounting.
- [x] Connect app_structure must not swallow typed authorization failures. Global discovery 403 must not masquerade as opportunity-specific denial.
- [x] Exercise these paths with real database memberships and mocked upstream responses; test existing access gate denies after archival.

## Chunk 4: Verification and handoff

- [x] Run focused tests, related auth/identity/loader suites, Ruff, migration consistency and relevant architectural fitness checks.
- [x] Independent review for scope correctness and races; fix findings and rerun affected tests.
- [x] Create a small reviewable PR with evidence and explicit rollout boundary; do not merge or deploy automatically.
- [x] Correct the local triage completion display separately from the security PR.

## Review refinements

All denial observations advance the connection timestamp, including tenant-only403, so earlier discovery cannot restore access. Explicitly different legacy OCS OAuth teams are preserved; API-key401 remains credential-wide. Refresh rejection also verifies the refresh secret/app snapshot, and materialization does not re-record an already handled rejection using weaker evidence. Existing-account discovery runs on allauth pre_social_login after token storage (and user reconciliation), while new connects retain social_account_added. The actual callback order has a regression test.

This PR leaves the current any-of read gate intact: mixed-coverage workspace enforcement is #380, and ambiguous legacy-team cleanup is #379. Global Connect metadata list403 is not treated as an opportunity-specific denial. A successful complete discovery restores confirmed memberships; a successful token refresh alone does not.

Final verification: 285 related tests passed; Ruff lint/format, migration consistency, Django system checks and diff whitespace checks passed.

Review PR: https://github.com/dimagi-rad/scout/pull/441 (open; not merged or deployed).

Further critical review reproduced a denial/discovery race: clearing the timestamp allowed an old response to restore access after a newer discovery. Retain the last-denial timestamp permanently as the observation fence, while clearing the active code on successful discovery. Regression failed before the fix; 285 related tests pass afterward. Independent re-review found no remaining introduced actionable issues. Claude auto-review must produce a usable result before requesting snopoke.
