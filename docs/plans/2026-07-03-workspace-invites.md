# Workspace Invites (Root Cause C) — Task Plan

> **For Claude:** Execute task-by-task with TDD (superpowers:test-driven-development): failing test first, watch it fail, minimal code, watch it pass, commit. One logical change per commit.

**Goal:** Let a workspace manager invite an email that has no Scout account (or an account lacking live upstream access). An email-keyed `WorkspaceInvite` auto-resolves into a `WorkspaceMembership` when that person logs in AND has live tenant access (A's `access.py` gate), with bidirectional notifications when they log in but lack upstream access.

**Architecture:** Email-keyed `WorkspaceInvite` (never placeholder Users). Unified add-member endpoint branches on target existence + live-tenant access. Post-login resolver runs at the end of `resolve_tenant_on_social_login`. Email via django-anymail + Amazon SES (console in dev), sent async through Procrastinate.

**Tech Stack:** Django 5 async, DRF `APIView` (sync), Procrastinate, django-allauth, django-anymail[amazon-ses], React 19 frontend.

Source design (locked): `/Users/bderenzi/Code/dimagi/scout/docs/superpowers/specs/2026-06-18-workspace-invites-plan.md`

---

## Phase 1 — model + unified endpoint + resolver

### Task 1: `WorkspaceInvite` model + migration
- Modify: `apps/workspaces/models.py` — add `WorkspaceInviteStatus`, `WorkspaceInvite`, `INVITE_TTL_DAYS`, `default_invite_expiry`.
- Test: `tests/test_workspace_invite_model.py` — create invite; `is_expired`; the conditional unique constraint (two live invites for same (workspace,email) → IntegrityError; a revoked + a new pending allowed).
- Migration: `apps/workspaces/migrations/` (makemigrations).

### Task 2: unified add-member endpoint (3 branches) + GET includes invites
- Modify: `apps/workspaces/api/workspace_views.py` `WorkspaceMemberListView` (get/post).
- Response POST gains `result` discriminator: `member` | `invite_pending` | `invite_awaiting_access`.
- GET returns `{"members": [...], "invites": [...]}`.
- Reuse `access._shares_live_tenant` for the target check (DRY, keeps authorizer as gate).
- Idempotent re-invite (update role/expiry on a live invite; fresh invite if only terminal ones exist).
- Test: extend `tests/test_workspace_management.py` (`TestMemberAdd`) — update the old 404/403 tests to invite branches; add GET-with-invites.
- Keep `tests/test_authorizer_is_sole_gate.py` green (`# authz-exempt` on the target-membership check).

### Task 3: invite revoke + role change endpoint
- Add: `WorkspaceInviteDetailView` in `workspace_views.py`; route `invites/<uuid:invite_id>/` in `config/urls.py`.
- DELETE → status=revoked (manager only). PATCH role → update live invite role (manager only).
- Test: `tests/test_workspace_management.py` new `TestInviteDetail`.

### Task 4: post-login resolver
- Modify: `apps/users/signals.py` — add `resolve_pending_invites_on_login(user)`; call at end of `resolve_tenant_on_social_login` (always, in try/except).
- Match by verified `EmailAddress` set + `user.email`. Reuse `access._shares_live_tenant` / `_live_tenant_ids`.
- Test: `tests/test_invite_resolution.py` — pending→accepted; pending→awaiting_access; awaiting_access→accepted; verified-email match; expired/revoked skip; resolver never breaks login.

### Task 5: frontend invite rows + dialog + banner
- Modify: `frontend/src/api/workspaces.ts`, `WorkspaceDetailPage`, `WorkspacesPage`.
- Invite rows w/ status chips, revoke button, role selector; add-member dialog handles 3 result shapes; awaiting_access banner on workspace list. `data-testid` per convention.

## Phase 2 — email infra + invite email

### Task 6: anymail + SES settings + Procrastinate send_email task
- Modify: `pyproject.toml` (`django-anymail[amazon-ses]`), `uv lock`.
- Settings: `base.py` (EMAIL_TIMEOUT, SERVER_EMAIL, EMAIL_SUBJECT_PREFIX, DEFAULT_FROM_EMAIL default), `production.py` (anymail amazon_ses + `ANYMAIL={}`), `SCOUT_BASE_URL`.
- Add: `apps/users/tasks.py` `send_email` (wraps `django.core.mail.send_mail`) via `@task` from `config.procrastinate`.
- Test: `tests/test_email_task.py` — task sends to `mail.outbox`.

### Task 7: send pending invite email
- Add: `apps/workspaces/services/invite_notifications.py` — `send_pending_invite_email(invite)`; deep link `{SCOUT_BASE_URL}/?invite={token}`.
- Wire into endpoint pending branch.
- Test: assert `mail.outbox` on POST pending branch.

## Phase 3 — bidirectional notifications

### Task 8: awaiting_access + accepted notifications, generic data-source naming
- `describe_workspace_sources(workspace)` generic labels (Connect opportunity / OCS bot / CommCare HQ project).
- Resolver fires: invitee (in-app + email) + manager (email) on awaiting_access; both on accepted.
- In-app: `GET /api/invites/` returns current user's awaiting_access invites w/ rendered message; frontend banner.
- Test: `tests/test_invite_notifications.py` — outbox contents per transition; in-app endpoint.

## Guardrails
- `tests/test_authorizer_is_sole_gate.py` and `tests/test_async_conventions.py` stay green.
- `makemigrations --check` clean. Module-level imports incl. tests. Comment why-not-what.
- One PR → auto-review → add snopoke. Do NOT self-merge. SES ops checklist in PR body.
