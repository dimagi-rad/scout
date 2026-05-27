# Cross-provider account linking by email

## Problem

A single user logging in through different OAuth providers (CommCare HQ, CommCare Connect, Open Chat Studio) ends up with a separate `User` row per provider, even when each provider returns the same email address. Two issues:

1. **No cross-provider linking.** `SOCIALACCOUNT_EMAIL_AUTHENTICATION` is unset (defaults to `False`) and no provider in `SOCIALACCOUNT_PROVIDERS` declares `VERIFIED_EMAIL: True`. allauth's `_lookup_by_email` gate (`allauth/socialaccount/models.py:386`) therefore never fires, so each new provider login creates a brand-new `User` row.
2. **Connect-only users have NULL emails.** Connect's `/api/users/me/` doesn't currently return an email field, so `extract_common_fields` in `apps/users/providers/commcare_connect/provider.py:48` writes `email=None`. These users render as `user-47` in the UI.

Connect is going to start returning email in their API (separate work, outside this spec). Once they do, we need pre-existing email-less Connect User rows to absorb that email — and to merge into any other Scout user already owning that email.

## Scope

In scope:
- Enable allauth's email-based auto-link for the three Dimagi providers.
- Backfill `User.email` on subsequent OAuth logins when a provider belatedly supplies one.
- Auto-merge mid-login when the supplied email collides with another existing user.
- A one-off management command for explicit/admin-driven merges of existing duplicates in prod.

Out of scope:
- Connect API changes (handled by the Connect team).
- Querying Connect for emails of users who never log in again.
- Cross-email merges (user changed email between providers — manual `--canonical-id` only).
- UI improvements for accounts still rendering as `user-47`.

## Approach

Three small pieces working together:

| Piece | Where | What it does |
|---|---|---|
| **A — settings** | `config/settings/base.py` | Mark the 3 Dimagi providers' emails as verified and turn on allauth's email-authentication gate. New OAuth logins with email auto-link to an existing email-owning user. |
| **B — `pre_social_login` handler** | `apps/users/signals.py` | For repeat OAuth logins on existing User rows (where allauth short-circuits before email lookup), either backfill `User.email` or trigger an inline merge. |
| **C — merge service** | `apps/users/services/merge.py` | One shared merge implementation called by both the signal handler and the management command. |
| **D — merge command** | `apps/users/management/commands/merge_duplicate_users.py` | Operator-facing entry point for explicit/admin-driven merges of existing duplicates. |

A handles new account links. B handles existing email-less rows that become email-bearing later (the Connect rollout case). C is the building block for B. D cleans up duplicates that already exist in prod.

## Detailed design

### A. Settings change

`config/settings/base.py`:

```python
# Treat the verified email returned by trusted Dimagi OAuth providers as
# authoritative for matching against an existing local user account. Enables
# allauth's _lookup_by_email flow.
SOCIALACCOUNT_EMAIL_AUTHENTICATION = True

SOCIALACCOUNT_PROVIDERS = {
    # ... unchanged google, github entries ...
    "commcare_connect": {"OAUTH_PKCE_ENABLED": True, "VERIFIED_EMAIL": True},
    "commcare": {"OAUTH_PKCE_ENABLED": True, "VERIFIED_EMAIL": True},
    "ocs": {"OAUTH_PKCE_ENABLED": True, "VERIFIED_EMAIL": True},
}
```

`SOCIALACCOUNT_EMAIL_AUTHENTICATION_AUTO_CONNECT = True` is already set (`base.py:197`), so once an email match succeeds allauth attaches the new `SocialAccount` to the matched user automatically. No further wiring needed for the first-time-link path.

### B. `pre_social_login` handler

New receiver in `apps/users/signals.py`, wired in `apps/users/apps.py` alongside the existing `resolve_tenant_on_social_login` connect:

```python
@receiver(pre_social_login)
def reconcile_existing_user_on_login(sender, request, sociallogin, **kwargs):
    """
    When an existing OAuth user (matched via SocialAccount uid) has the
    provider return an email for the first time, either backfill the User row
    or merge into the email-owning user.

    allauth's `_lookup_by_email` doesn't fire for repeat logins (the prior
    `_lookup_by_socialaccount` short-circuits in models.py:340), so this
    handler closes that gap.
    """
    new_email = sociallogin.account.extra_data.get("email")
    if not new_email:
        return
    user = sociallogin.user
    if user.pk is None:
        return  # brand-new user; allauth's lookup_by_email handles it
    if user.email:
        return  # already has an email — nothing to reconcile

    canonical = User.objects.filter(email__iexact=new_email).exclude(pk=user.pk).first()
    if canonical is None:
        user.email = new_email
        user.save(update_fields=["email"])
        return

    try:
        merge_users(canonical=canonical, duplicate=user)
    except Exception:
        logger.exception(
            "Auto-merge failed for user=%s into canonical=%s", user.pk, canonical.pk,
        )
        return  # let login proceed on the duplicate row; admin can run command later
    sociallogin.user = canonical
    sociallogin.account.user = canonical
```

Branches:
- No email in extra_data → no-op.
- User already has an email → no-op.
- No collision → backfill `User.email`.
- Collision → call `merge_users(...)`, redirect `sociallogin.user` and `sociallogin.account.user` so the login session lands on the canonical row.

Merge failure never blocks login. The exception is logged at ERROR and the user lands on the duplicate row; admin can run the management command to clean up.

### C. Merge service

`apps/users/services/merge.py` exports:

```python
def merge_users(*, canonical: User, duplicate: User, dry_run: bool = False) -> MergeReport: ...
```

A single `transaction.atomic()` wraps the entire merge — partial failure rolls everything back. `MergeReport` is a dataclass holding counts and per-table notes for logging and for the management command's output.

**Field-level merge** applied to canonical:
- `password`: if canonical's is unusable (`has_usable_password() is False`) and duplicate's is usable, copy duplicate's hash.
- `is_staff`, `is_superuser`: logical OR.
- `last_login`: max of the two (handle `None`).
- `first_name`, `last_name`, `avatar_url`: fill from duplicate only when canonical's value is empty.
- `timezone`: fill from duplicate only when canonical is at default (`"UTC"`).
- `email`, `username`: untouched on canonical.

**FK repointing** — four tables with unique constraints involving the user FK get explicit conflict resolution; everything else is straight-update via `User._meta.related_objects` introspection.

| Table | Unique key | Resolution |
|---|---|---|
| `socialaccount.SocialAccount` | `(provider, uid)` | Straight repoint — different external users have different uids per provider, no collisions. |
| `account.EmailAddress` | `(email,)` | If both have a row for the same email, delete duplicate's. After repointing, ensure canonical has exactly one row matching `User.email`, marked `primary=True, verified=True`. |
| `users.TenantMembership` | `(user, tenant)` | If canonical already has a membership for that tenant, delete duplicate's (its `TenantCredential` cascades). Otherwise repoint. |
| `workspaces.WorkspaceMembership` | `(workspace, user)` | If canonical already has a membership for that workspace, keep canonical's row but upgrade `role` to the higher of the two per the explicit hierarchy below. Delete duplicate's row. Otherwise repoint. |

`WorkspaceRole` is a `TextChoices` enum (`apps/workspaces/models.py:101`) with values `READ`, `READ_WRITE`, `MANAGE`. The merge service defines an explicit ordering map `{READ: 0, READ_WRITE: 1, MANAGE: 2}` and picks the higher-ranked role on conflict.

**Introspection loop** — iterates `User._meta.related_objects`, skips the four special-case relations above, and runs `Model.objects.filter(field=duplicate).update(field=canonical)` for each. Covers:
- `Workspace.created_by`, `WorkspaceMembership.invited_by`
- `chat.Thread.user`
- `artifacts.Artifact.created_by`, `artifacts.Artifact.deleted_by`
- `transformations.*.created_by`
- `recipes.*.created_by`, `recipes.*.deleted_by`, `recipes.*.run_by`
- `knowledge.*.updated_by`, `knowledge.*.created_by`, `knowledge.*.discovered_by_user`
- Django's `auth_user_groups`, `auth_user_user_permissions`, `django_admin_log`

When a new app adds a `User` FK in the future, the loop picks it up automatically. Only when the new FK has a unique constraint involving `user` does the merge service need to be updated (the introspection loop will raise an `IntegrityError`, surfacing the need clearly).

Final step: delete the duplicate `User` row.

`dry_run=True` builds the same plan and returns a populated `MergeReport`, but performs no writes (and does not enter `atomic()` at all — easier to reason about than execute-and-raise).

### D. Merge command

`apps/users/management/commands/merge_duplicate_users.py`:

```
python manage.py merge_duplicate_users [--dry-run] [--email EMAIL]
                                       [--canonical-id ID] [--yes]
```

| Flag | Effect |
|---|---|
| (none) | Find every email group with >1 user, merge each |
| `--dry-run` | Print plan, write nothing |
| `--email` | Only operate on the group sharing that email (case-insensitive) |
| `--canonical-id` | Force this user as the survivor (must be in the targeted group) |
| `--yes` | Skip the interactive `[y/N]` confirmation |

**Canonical selection** (default tiebreak, when not forced):
1. Has a usable password (preserves email/password login capability).
2. Oldest `created_at`.
3. Lowest `pk`.

Users with `email IS NULL` are never grouped — no meaningful match key.

**Flow:**
1. Build list of duplicate groups (or single group if `--email`).
2. If `--email` was supplied and the targeted email has <2 users, exit 0 with `no duplicates found for <email>` — not an error.
3. For each group, pick canonical, build a plan via `merge_users(dry_run=True)`.
4. Print all plans.
5. If not `--dry-run` and not `--yes`, prompt `Continue? [y/N]`. Empty input or anything other than `y`/`Y` aborts.
6. For each group, call `merge_users(...)` for real. Exceptions in one group are logged and skipped; remaining groups proceed.

**Per-group output:**

```
[merge] email='brian@acompahealth.com'  3 users found
  canonical: User#12  created=2026-03-14  providers=[ocs, commcare_connect]  has_password=True
  duplicates: User#47, User#52
  plan:
    SocialAccount         repoint 2 (commcare, github)
    EmailAddress          delete 2 (canonical already has primary+verified row)
    TenantMembership      repoint 4, conflict-delete 1
    WorkspaceMembership   repoint 3, conflict-merge 1 (role: READ → MANAGE)
    Workspace.created_by  repoint 2
    chat.Thread           repoint 11
    [...]
    User                  copy password from #47, OR is_staff(False|True)=True
  → would delete User#47, User#52
```

Real-mode last line: `→ deleted User#47, User#52`.

## Trust model

`VERIFIED_EMAIL: True` on `commcare`, `commcare_connect`, and `ocs` declares that Dimagi vouches for the email's verification status. This is the security trade-off allauth warns about: an untrustworthy provider could fabricate email data to gain access to another user's account.

For Scout this is acceptable because all three providers are operated by Dimagi. For Connect specifically, the flag is dormant today (Connect returns no email field at all); it becomes load-bearing only once Connect ships email. If Connect's email rollout does not include server-side verification, this assumption will need to be revisited.

## Edge cases

- **Brand-new social login (`sociallogin.user.pk is None`)** — handler returns early; allauth's `_lookup_by_email` is the authoritative path.
- **User already has email** — handler returns early; we don't overwrite an existing email even if the provider's differs (could indicate the user changed email upstream, which we don't auto-handle).
- **Case mismatch** — `email__iexact` lookup so `Brian@Foo.com` matches `brian@foo.com`.
- **Three-way duplicates** — pick one canonical, merge the others into it sequentially in the same command run.
- **Multi-tab session** — when auto-merge fires mid-login, an open session on the duplicate row in another browser/tab becomes invalid on its next request (Django can't load the deleted user). Acceptable.
- **Connect-only users who never re-log-in** — stay split. Not addressable without external user data.
- **Merge failure** — signal handler catches and logs; login proceeds on the duplicate row. Management command's `atomic()` rolls back the affected group, others still proceed.

## Testing

Two new files; no changes to existing `tests/test_auth.py` beyond regression spot-checks.

### `tests/test_social_login_reconciliation.py`

Covers the `pre_social_login` handler with constructed `SocialLogin` fixtures (no live OAuth).

- `test_brand_new_user_is_noop`
- `test_existing_user_with_email_is_noop`
- `test_no_email_in_extra_data_is_noop`
- `test_no_collision_backfills_email`
- `test_collision_triggers_merge_and_redirects_session`
- `test_merge_failure_does_not_break_login` (patch `merge_users` to raise)
- `test_case_insensitive_email_collision_match`

### `tests/test_merge_users_service.py`

Direct unit tests on `merge_users` without OAuth machinery.

- `test_canonical_selection_prefers_usable_password`
- `test_canonical_selection_prefers_oldest_when_passwords_equal`
- `test_socialaccounts_repointed`
- `test_emailaddress_dedupes_when_both_have_same_email`
- `test_tenantmembership_repoint_with_no_overlap`
- `test_tenantmembership_conflict_keeps_canonical_membership`
- `test_workspacemembership_conflict_keeps_higher_role`
- `test_chat_threads_repointed_via_introspection`
- `test_setnull_fks_repointed_via_introspection`
- `test_field_level_merge_copies_password`
- `test_field_level_merge_or_staff_flags`
- `test_duplicate_user_row_deleted`
- `test_atomic_rollback_on_failure`
- `test_dry_run_writes_nothing`

The management command's interactive `[y/N]` prompt is not unit-tested; the underlying service has full coverage and the command is a thin wrapper.

## Rollout

1. Ship the settings + signal handler + merge service + command in one PR.
2. After deploy, run `python manage.py merge_duplicate_users --dry-run` in prod to preview the full sweep.
3. Execute the sweep — either targeted (`--email brian@acompahealth.com` to start with one known case) or global (no flags). Both invoke the same underlying service; targeted is recommended for the first run.
4. From this point on, new logins auto-link via email (path A). Existing email-less Connect rows auto-heal on next login once Connect ships email (path B).

### Observability

- Auto-merges (path B collision branch) log at INFO: `"auto-merged user=X into canonical=Y email=...@... provider=..."`.
- Auto-merge failures log at ERROR with traceback.
- The management command prints a per-group summary to stdout.

Grep `auto-merge` in production logs to audit handler behavior over time.
