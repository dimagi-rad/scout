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
- [x] In `token_refresh.py`, refresh rejection records a reconnect fingerprint without archiving memberships: invalid_grant can mean a rotated/stale refresh secret. Preserve the typed reconnect distinction; resource/discovery 401/403 own archival.

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

All denial observations advance the connection timestamp, including tenant-only403, so earlier discovery cannot restore access. Explicitly different legacy OCS OAuth teams are preserved; API-key401 remains credential-wide. Refresh rejection retains the credential fingerprint and never archives access; materialization does not reinterpret it as a resource denial. Existing-account discovery runs on allauth pre_social_login after token storage (and user reconciliation), while new connects retain social_account_added. The actual callback order has a regression test.

This PR leaves the current any-of read gate intact: mixed-coverage workspace enforcement is #380, and ambiguous legacy-team cleanup is #379. Global Connect metadata list403 is not treated as an opportunity-specific denial. A successful complete discovery restores confirmed memberships; a successful token refresh alone does not.

Final verification: 285 related tests passed; Ruff lint/format, migration consistency, Django system checks and diff whitespace checks passed.

Review PR: https://github.com/dimagi-rad/scout/pull/441 (open; not merged or deployed).

Further critical review reproduced a denial/discovery race: clearing the timestamp allowed an old response to restore access after a newer discovery. Retain the last-denial timestamp permanently as the observation fence, while clearing the active code on successful discovery. Regression failed before the fix; 285 related tests pass afterward. Independent re-review found no remaining introduced actionable issues. Claude auto-review must produce a usable result before requesting snopoke.

## Takeover review iteration — 14 September

Claude’s refresh-grant finding was confirmed with six failing sync/async regression cases. Refresh rejection now preserves memberships and the reconnect signal. Expected HTTP/transport refresh failures carry AUTH_REFRESH_FAILED and retry guidance at the loader boundary; unexpected defects remain ordinary errors. Existing token-endpoint error logging for outages is retained, as required by the existing lifecycle tests; expected loader errors do not add a second Sentry failure.

The allauth regression now drives complete_social_login through real login and existing-account connect flows, checking the stored token, outgoing bearer header, authenticated session and restored membership. Discovery guard skips are logged, CommCare auth status is explicit, and empty denial-code writes are skipped.

Internal review reproduced an additional race: discovery begun without a connection could restore access after newer discovery and denial. Discovery now preserves whether the connection existed at request start and verifies the incoming credential before replacing an identity. Regression coverage includes removed/rotated replacement tokens.

The durable code records unresolved connection-wide denial for later coverage UI; tenant-scoped denials remain represented by archived memberships. The timestamp is a permanent ordering fence, not a current-denial boolean. UI consumption, periodic reconciliation, usable-credential coverage and all-of enforcement remain subsequent section-02 work.

Takeover validation: 336 related PostgreSQL tests passed; 56 affected tests passed after final refresh-classification refinement. Ruff lint/format, migration consistency, Django system checks and diff checks passed. Updated Claude review remains the final automated review gate.

## Second Claude pass

Claude confirmed the initial seven findings and discovery races were addressed. Its next pass identified inconsistent treatment of Connect's global org/program-list 403. Discovery now matches the loader: this response has unknown opportunity scope, so it raises without archiving memberships. A 401 remains connection-wide and a per-opportunity 403 remains tenant-specific. The changed regression failed on the previous head.

Denial-recorder declines now log their reason and connection ID; the scope marker is declared with shared enum values. Persistent refresh failure guidance points to administrator configuration checks. API-key recovery currently requires re-entering a validated key; the existing persistence flow restores memberships onto a new connection. Automatic API-key revalidation and retiring superseded denied connections are explicit follow-ups for periodic reconciliation/coverage UI, before that UI consumes denial state.

## Recovery policy confirmation after third Claude pass

Retain the approved tenant-membership recovery contract: a complete successful discovery begun after denial restores a listed membership, including a prior tenant-specific403, using the same credential. A regression now pins that case directly. This is not proof that the previously denied loader/export endpoint is usable; Scout's membership grain is domain/opportunity/chatbot, while each loader uses several endpoints. Independent critical review agreed that persistent endpoint denial would require its own recovery probe and lifecycle and is not a newly introduced grant relative to main. End-to-end export permission equivalence remains a limitation of the membership model, not a claim this PR makes or completes.

Pre-flight credential resolution now preserves AUTH_REFRESH_FAILED for outages/configuration failures and AUTH_TOKEN_EXPIRED for rejected grants. Two regression cases failed before the change. The provider-specific discovery403 policy is explicit, stale recorder declines log at WARNING, and access-denied guidance states that reconnecting alone does not change upstream permissions.

The allauth lifecycle deliberately uses complete_social_login/pre_social_login after token persistence for existing identities and social_account_added for new connections. Standalone token rotation does not discover memberships or restore denied access; reconnect discovery and explicit identity refresh own that step. No unsupported social_account_updated listener is retained merely as a second trigger. Real callback tests cover the supported flows.
