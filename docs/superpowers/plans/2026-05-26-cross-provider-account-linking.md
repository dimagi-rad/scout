# Cross-provider account linking — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a single human user log in via CommCare HQ, CommCare Connect, or OCS and always land on the same `User` row, with a management command to merge existing duplicates.

**Architecture:** Four pieces. (A) allauth settings enable email-based auto-link for new OAuth signups. (B) A `pre_social_login` signal handler covers the repeat-login case where allauth short-circuits before its email lookup — either backfilling `User.email` or merging mid-login. (C) A shared `merge_users` service does the actual record merging in one atomic transaction. (D) A `manage.py merge_duplicate_users` command provides operator-driven merges.

**Tech Stack:** Django 5 (async-first), django-allauth 65.x, pytest + pytest-django, ruff (line-length=100, py311).

**Spec:** `docs/superpowers/specs/2026-05-22-cross-provider-account-linking-design.md`

---

## File map

**Modified:**
- `config/settings/base.py` — three lines: `SOCIALACCOUNT_EMAIL_AUTHENTICATION`, plus `VERIFIED_EMAIL: True` on three providers
- `apps/users/signals.py` — add `reconcile_existing_user_on_login` receiver
- `apps/users/apps.py` — wire the new signal alongside the existing two

**Created:**
- `apps/users/services/merge.py` — `MergeReport` dataclass, `select_canonical`, `merge_users`, and internal helpers
- `apps/users/management/commands/merge_duplicate_users.py` — operator-facing CLI
- `tests/test_merge_users_service.py` — direct unit tests on the service
- `tests/test_social_login_reconciliation.py` — handler tests using constructed `SocialLogin` mocks

---

## Phase 1 — Settings

### Task 1: Enable email-based auto-link in allauth

**Files:**
- Modify: `config/settings/base.py:183-229`
- Test: `tests/test_auth_settings.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/test_auth_settings.py`:

```python
"""Pin the auth settings that enable cross-provider account linking by email."""

from django.conf import settings


def test_email_authentication_is_enabled():
    assert settings.SOCIALACCOUNT_EMAIL_AUTHENTICATION is True


def test_email_authentication_auto_connect_is_enabled():
    assert settings.SOCIALACCOUNT_EMAIL_AUTHENTICATION_AUTO_CONNECT is True


def test_dimagi_providers_have_verified_email():
    providers = settings.SOCIALACCOUNT_PROVIDERS
    for pid in ("commcare", "commcare_connect", "ocs"):
        assert providers[pid].get("VERIFIED_EMAIL") is True, (
            f"{pid} must declare VERIFIED_EMAIL=True so allauth treats its emails as verified"
        )
```

- [ ] **Step 2: Run test to verify it fails**

```
uv run pytest tests/test_auth_settings.py -v
```

Expected: `test_email_authentication_is_enabled` FAILS (`AttributeError` or `False`), `test_dimagi_providers_have_verified_email` FAILS.

- [ ] **Step 3: Update settings**

Edit `config/settings/base.py`. After the existing `SOCIALACCOUNT_EMAIL_AUTHENTICATION_AUTO_CONNECT = True` line (around line 197), add:

```python
# Trust Dimagi-operated providers to have verified the email address on their
# end. Required so allauth's _lookup_by_email gate fires for these providers.
SOCIALACCOUNT_EMAIL_AUTHENTICATION = True
```

Then update `SOCIALACCOUNT_PROVIDERS` (currently 3 entries: `commcare_connect`, `commcare`, `ocs`) to add `"VERIFIED_EMAIL": True` to each:

```python
SOCIALACCOUNT_PROVIDERS = {
    "commcare_connect": {
        "OAUTH_PKCE_ENABLED": True,
        "VERIFIED_EMAIL": True,
    },
    "commcare": {
        "OAUTH_PKCE_ENABLED": True,
        "VERIFIED_EMAIL": True,
    },
    "ocs": {
        "OAUTH_PKCE_ENABLED": True,
        "VERIFIED_EMAIL": True,
    },
}
```

- [ ] **Step 4: Run test to verify it passes**

```
uv run pytest tests/test_auth_settings.py -v
```

Expected: all 3 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add config/settings/base.py tests/test_auth_settings.py
git commit -m "feat(auth): enable email-based auto-link across Dimagi OAuth providers"
```

---

## Phase 2 — Merge service

The merge service is built up test-by-test. Each task creates one focused capability and a test that pins it down. By the end, `merge_users(canonical, duplicate)` is fully functional.

### Task 2: Service skeleton + canonical selection

**Files:**
- Create: `apps/users/services/merge.py`
- Test: `tests/test_merge_users_service.py` (new)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_merge_users_service.py`:

```python
"""Unit tests for apps.users.services.merge.merge_users and helpers."""

import pytest
from django.contrib.auth import get_user_model

from apps.users.services.merge import select_canonical

User = get_user_model()


@pytest.mark.django_db
def test_select_canonical_prefers_usable_password():
    no_pw = User.objects.create(email="x@y.com", username="a")
    no_pw.set_unusable_password()
    no_pw.save()
    with_pw = User.objects.create(email="x@y.com", username="b")
    with_pw.set_password("real-password")
    with_pw.save()

    assert select_canonical([no_pw, with_pw]) == with_pw


@pytest.mark.django_db
def test_select_canonical_prefers_oldest_when_passwords_equal():
    older = User.objects.create(email="x@y.com", username="older")
    older.set_password("pw")
    older.save()
    newer = User.objects.create(email="x@y.com", username="newer")
    newer.set_password("pw")
    newer.save()

    assert select_canonical([newer, older]) == older
```

Note: `email` is nullable+unique in this codebase, but we set the same string here because `User.save()` only stores the email when the `__init__` value is provided; we'll only ever pass groups of duplicates that already share a real email through `select_canonical`.

- [ ] **Step 2: Run tests to verify they fail**

```
uv run pytest tests/test_merge_users_service.py -v
```

Expected: ImportError — `apps.users.services.merge` doesn't exist yet.

Wait — `User.email` has `unique=True`. Creating two users with the same email will violate the constraint. The above test won't work as-written.

Re-write the tests using distinct emails (the helper only orders within a group; the actual email-grouping is done by the command, not the helper):

```python
@pytest.mark.django_db
def test_select_canonical_prefers_usable_password():
    no_pw = User.objects.create(email="a@y.com", username="a")
    no_pw.set_unusable_password()
    no_pw.save()
    with_pw = User.objects.create(email="b@y.com", username="b")
    with_pw.set_password("real-password")
    with_pw.save()

    assert select_canonical([no_pw, with_pw]) == with_pw


@pytest.mark.django_db
def test_select_canonical_prefers_oldest_when_passwords_equal():
    older = User.objects.create(email="older@y.com", username="older")
    older.set_password("pw")
    older.save()
    newer = User.objects.create(email="newer@y.com", username="newer")
    newer.set_password("pw")
    newer.save()

    assert select_canonical([newer, older]) == older
```

- [ ] **Step 3: Write minimal implementation**

Create `apps/users/services/merge.py`:

```python
"""Merge two duplicate User rows into one canonical row.

Used by:
- ``apps.users.signals.reconcile_existing_user_on_login`` to absorb a freshly
  email-bearing OAuth user into an existing email-owning user during login.
- ``manage.py merge_duplicate_users`` for operator-driven cleanup.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from django.contrib.auth import get_user_model

if TYPE_CHECKING:
    from apps.users.models import User

logger = logging.getLogger(__name__)


@dataclass
class MergeReport:
    """Per-merge summary used for logging and command output."""

    canonical_id: int
    duplicate_id: int
    dry_run: bool = False
    field_changes: dict[str, str] = field(default_factory=dict)
    socialaccount_repointed: int = 0
    emailaddress_repointed: int = 0
    emailaddress_deleted: int = 0
    tenant_membership_repointed: int = 0
    tenant_membership_conflict_deleted: int = 0
    workspace_membership_repointed: int = 0
    workspace_membership_conflict_merged: int = 0
    long_tail_fk_counts: dict[str, int] = field(default_factory=dict)
    duplicate_user_deleted: bool = False


def select_canonical(users: list["User"]) -> "User":
    """Return the canonical user from a list of duplicates.

    Priority (highest wins): has a usable password, oldest ``created_at``,
    lowest ``pk``.
    """
    return min(
        users,
        key=lambda u: (
            not u.has_usable_password(),
            u.created_at,
            u.pk,
        ),
    )


def merge_users(
    *,
    canonical: "User",
    duplicate: "User",
    dry_run: bool = False,
) -> MergeReport:
    """Merge ``duplicate`` into ``canonical`` and return a MergeReport.

    Implementation arrives across the remaining tasks in this phase.
    """
    if canonical.pk == duplicate.pk:
        raise ValueError("canonical and duplicate must be different users")
    return MergeReport(
        canonical_id=canonical.pk,
        duplicate_id=duplicate.pk,
        dry_run=dry_run,
    )
```

Also create `apps/users/services/__init__.py` (likely already exists from earlier imports).

- [ ] **Step 4: Run tests to verify they pass**

```
uv run pytest tests/test_merge_users_service.py -v
```

Expected: both tests PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/users/services/merge.py tests/test_merge_users_service.py
git commit -m "feat(users): scaffold merge_users service with canonical selection"
```

---

### Task 3: User field-level merge

**Files:**
- Modify: `apps/users/services/merge.py`
- Modify: `tests/test_merge_users_service.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_merge_users_service.py`:

```python
from apps.users.services.merge import merge_users


@pytest.mark.django_db
def test_field_level_merge_copies_password_from_duplicate():
    canonical = User.objects.create(email="canon@y.com", username="canon")
    canonical.set_unusable_password()
    canonical.save()
    duplicate = User.objects.create(email="dup@y.com", username="dup")
    duplicate.set_password("real-password")
    duplicate.save()
    dup_hash = duplicate.password

    merge_users(canonical=canonical, duplicate=duplicate)

    canonical.refresh_from_db()
    assert canonical.password == dup_hash
    assert canonical.has_usable_password()


@pytest.mark.django_db
def test_field_level_merge_ors_staff_and_superuser_flags():
    canonical = User.objects.create(email="canon@y.com", username="canon")
    duplicate = User.objects.create(
        email="dup@y.com", username="dup", is_staff=True, is_superuser=True,
    )

    merge_users(canonical=canonical, duplicate=duplicate)

    canonical.refresh_from_db()
    assert canonical.is_staff is True
    assert canonical.is_superuser is True


@pytest.mark.django_db
def test_field_level_merge_fills_empty_name_fields_from_duplicate():
    canonical = User.objects.create(email="canon@y.com", username="canon")
    duplicate = User.objects.create(
        email="dup@y.com", username="dup",
        first_name="Brian", last_name="DeRenzi", avatar_url="https://x/y.png",
    )

    merge_users(canonical=canonical, duplicate=duplicate)

    canonical.refresh_from_db()
    assert canonical.first_name == "Brian"
    assert canonical.last_name == "DeRenzi"
    assert canonical.avatar_url == "https://x/y.png"


@pytest.mark.django_db
def test_field_level_merge_keeps_canonical_name_when_already_set():
    canonical = User.objects.create(
        email="canon@y.com", username="canon", first_name="Already", last_name="Set",
    )
    duplicate = User.objects.create(
        email="dup@y.com", username="dup", first_name="Newer", last_name="Name",
    )

    merge_users(canonical=canonical, duplicate=duplicate)

    canonical.refresh_from_db()
    assert canonical.first_name == "Already"
    assert canonical.last_name == "Set"
```

- [ ] **Step 2: Run tests to verify they fail**

```
uv run pytest tests/test_merge_users_service.py -v
```

Expected: the four new tests FAIL (no field-level merge runs yet).

- [ ] **Step 3: Implement field-level merge**

In `apps/users/services/merge.py`, add at module level (above `merge_users`):

```python
def _merge_user_fields(canonical: "User", duplicate: "User") -> dict[str, str]:
    """Apply field-level merge rules. Mutates canonical in place; returns changes."""
    changes: dict[str, str] = {}
    if not canonical.has_usable_password() and duplicate.has_usable_password():
        canonical.password = duplicate.password
        changes["password"] = "copied from duplicate"
    if duplicate.is_staff and not canonical.is_staff:
        canonical.is_staff = True
        changes["is_staff"] = "True (OR with duplicate)"
    if duplicate.is_superuser and not canonical.is_superuser:
        canonical.is_superuser = True
        changes["is_superuser"] = "True (OR with duplicate)"
    if duplicate.last_login and (
        canonical.last_login is None or duplicate.last_login > canonical.last_login
    ):
        canonical.last_login = duplicate.last_login
        changes["last_login"] = f"{duplicate.last_login.isoformat()}"
    for field_name in ("first_name", "last_name", "avatar_url"):
        if not getattr(canonical, field_name) and getattr(duplicate, field_name):
            setattr(canonical, field_name, getattr(duplicate, field_name))
            changes[field_name] = f"copied: {getattr(duplicate, field_name)!r}"
    if canonical.timezone == "UTC" and duplicate.timezone and duplicate.timezone != "UTC":
        canonical.timezone = duplicate.timezone
        changes["timezone"] = f"copied: {duplicate.timezone!r}"
    canonical.save()
    return changes
```

Then modify `merge_users` to call it:

```python
def merge_users(*, canonical, duplicate, dry_run=False):
    if canonical.pk == duplicate.pk:
        raise ValueError("canonical and duplicate must be different users")
    report = MergeReport(
        canonical_id=canonical.pk,
        duplicate_id=duplicate.pk,
        dry_run=dry_run,
    )
    if dry_run:
        return report
    report.field_changes = _merge_user_fields(canonical, duplicate)
    return report
```

- [ ] **Step 4: Run tests to verify they pass**

```
uv run pytest tests/test_merge_users_service.py -v
```

Expected: all 6 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/users/services/merge.py tests/test_merge_users_service.py
git commit -m "feat(users): merge user-row fields (password, flags, last_login, names)"
```

---

### Task 4: Repoint SocialAccount rows

**Files:**
- Modify: `apps/users/services/merge.py`
- Modify: `tests/test_merge_users_service.py`

- [ ] **Step 1: Write failing test**

Append to `tests/test_merge_users_service.py`:

```python
from allauth.socialaccount.models import SocialAccount


@pytest.mark.django_db
def test_socialaccounts_are_repointed_to_canonical():
    canonical = User.objects.create(email="canon@y.com", username="canon")
    duplicate = User.objects.create(email="dup@y.com", username="dup")
    SocialAccount.objects.create(user=duplicate, provider="commcare", uid="42")
    SocialAccount.objects.create(user=duplicate, provider="ocs", uid="ocs-7")
    SocialAccount.objects.create(user=canonical, provider="commcare_connect", uid="9")

    report = merge_users(canonical=canonical, duplicate=duplicate)

    assert report.socialaccount_repointed == 2
    assert SocialAccount.objects.filter(user=canonical).count() == 3
    assert SocialAccount.objects.filter(user=duplicate).count() == 0
```

- [ ] **Step 2: Run test to verify it fails**

```
uv run pytest tests/test_merge_users_service.py::test_socialaccounts_are_repointed_to_canonical -v
```

Expected: FAIL — `socialaccount_repointed == 0`, no rows moved.

- [ ] **Step 3: Implement repointing**

Add to `apps/users/services/merge.py` (module level imports first):

```python
from django.db import transaction
from allauth.socialaccount.models import SocialAccount
```

Add helper:

```python
def _repoint_social_accounts(canonical: "User", duplicate: "User") -> int:
    return SocialAccount.objects.filter(user=duplicate).update(user=canonical)
```

Wrap `merge_users` body in a transaction and call the helper:

```python
def merge_users(*, canonical, duplicate, dry_run=False):
    if canonical.pk == duplicate.pk:
        raise ValueError("canonical and duplicate must be different users")
    report = MergeReport(
        canonical_id=canonical.pk, duplicate_id=duplicate.pk, dry_run=dry_run,
    )
    if dry_run:
        report.socialaccount_repointed = SocialAccount.objects.filter(user=duplicate).count()
        return report
    with transaction.atomic():
        report.field_changes = _merge_user_fields(canonical, duplicate)
        report.socialaccount_repointed = _repoint_social_accounts(canonical, duplicate)
    return report
```

- [ ] **Step 4: Run tests to verify they pass**

```
uv run pytest tests/test_merge_users_service.py -v
```

Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/users/services/merge.py tests/test_merge_users_service.py
git commit -m "feat(users): repoint SocialAccount rows during merge"
```

---

### Task 5: Dedupe EmailAddress rows

**Files:**
- Modify: `apps/users/services/merge.py`
- Modify: `tests/test_merge_users_service.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_merge_users_service.py`:

```python
from allauth.account.models import EmailAddress


@pytest.mark.django_db
def test_emailaddress_dedupes_when_both_have_same_email():
    canonical = User.objects.create(email="brian@y.com", username="canon")
    duplicate = User.objects.create(email="brian-old@y.com", username="dup")
    EmailAddress.objects.create(user=canonical, email="brian@y.com", primary=True, verified=True)
    EmailAddress.objects.create(user=duplicate, email="brian@y.com", primary=True, verified=False)

    report = merge_users(canonical=canonical, duplicate=duplicate)

    assert report.emailaddress_deleted == 1
    assert EmailAddress.objects.filter(email="brian@y.com").count() == 1
    survivor = EmailAddress.objects.get(email="brian@y.com")
    assert survivor.user == canonical
    assert survivor.primary is True
    assert survivor.verified is True


@pytest.mark.django_db
def test_emailaddress_repoints_distinct_addresses():
    canonical = User.objects.create(email="brian@y.com", username="canon")
    duplicate = User.objects.create(email="brian-old@y.com", username="dup")
    EmailAddress.objects.create(user=duplicate, email="brian-old@y.com", primary=True, verified=True)

    report = merge_users(canonical=canonical, duplicate=duplicate)

    assert report.emailaddress_repointed == 1
    assert EmailAddress.objects.filter(user=canonical, email="brian-old@y.com").exists()


@pytest.mark.django_db
def test_emailaddress_creates_primary_row_for_canonical_email():
    canonical = User.objects.create(email="brian@y.com", username="canon")
    duplicate = User.objects.create(email="brian-old@y.com", username="dup")
    # No EmailAddress rows for canonical; merge should create one.

    merge_users(canonical=canonical, duplicate=duplicate)

    primary = EmailAddress.objects.get(user=canonical, email="brian@y.com")
    assert primary.primary is True
    assert primary.verified is True
```

- [ ] **Step 2: Run tests to verify they fail**

```
uv run pytest tests/test_merge_users_service.py -v
```

Expected: three new tests FAIL.

- [ ] **Step 3: Implement EmailAddress dedupe**

Add to `apps/users/services/merge.py` imports:

```python
from allauth.account.models import EmailAddress
```

Add helper:

```python
def _dedupe_email_addresses(canonical: "User", duplicate: "User") -> tuple[int, int]:
    """Returns (repointed_count, deleted_count)."""
    canonical_emails = set(
        EmailAddress.objects.filter(user=canonical).values_list("email", flat=True)
    )
    dup_qs = EmailAddress.objects.filter(user=duplicate)
    deleted, _ = dup_qs.filter(email__in=canonical_emails).delete()
    repointed = dup_qs.exclude(email__in=canonical_emails).update(user=canonical)
    if canonical.email:
        primary, _created = EmailAddress.objects.get_or_create(
            user=canonical, email=canonical.email,
            defaults={"primary": True, "verified": True},
        )
        if not primary.primary or not primary.verified:
            primary.primary = True
            primary.verified = True
            primary.save(update_fields=["primary", "verified"])
        EmailAddress.objects.filter(user=canonical).exclude(pk=primary.pk).filter(
            primary=True,
        ).update(primary=False)
    return repointed, deleted
```

Call it in `merge_users` after `_repoint_social_accounts`:

```python
        report.emailaddress_repointed, report.emailaddress_deleted = (
            _dedupe_email_addresses(canonical, duplicate)
        )
```

And in the `dry_run` branch:

```python
        canonical_emails = set(
            EmailAddress.objects.filter(user=canonical).values_list("email", flat=True)
        )
        dup_qs = EmailAddress.objects.filter(user=duplicate)
        report.emailaddress_deleted = dup_qs.filter(email__in=canonical_emails).count()
        report.emailaddress_repointed = dup_qs.exclude(email__in=canonical_emails).count()
```

- [ ] **Step 4: Run tests to verify they pass**

```
uv run pytest tests/test_merge_users_service.py -v
```

Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/users/services/merge.py tests/test_merge_users_service.py
git commit -m "feat(users): dedupe EmailAddress rows during merge"
```

---

### Task 6: Merge TenantMembership with conflict resolution

**Files:**
- Modify: `apps/users/services/merge.py`
- Modify: `tests/test_merge_users_service.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_merge_users_service.py`:

```python
from apps.users.models import Tenant, TenantCredential, TenantMembership


@pytest.mark.django_db
def test_tenantmembership_repoints_when_canonical_has_no_overlap():
    canonical = User.objects.create(email="canon@y.com", username="canon")
    duplicate = User.objects.create(email="dup@y.com", username="dup")
    t1 = Tenant.objects.create(provider="commcare", external_id="d1", canonical_name="D1")
    t2 = Tenant.objects.create(provider="commcare", external_id="d2", canonical_name="D2")
    TenantMembership.objects.create(user=duplicate, tenant=t1)
    TenantMembership.objects.create(user=duplicate, tenant=t2)

    report = merge_users(canonical=canonical, duplicate=duplicate)

    assert report.tenant_membership_repointed == 2
    assert report.tenant_membership_conflict_deleted == 0
    assert TenantMembership.objects.filter(user=canonical).count() == 2


@pytest.mark.django_db
def test_tenantmembership_conflict_keeps_canonical_and_deletes_duplicates():
    canonical = User.objects.create(email="canon@y.com", username="canon")
    duplicate = User.objects.create(email="dup@y.com", username="dup")
    shared = Tenant.objects.create(provider="commcare", external_id="d1", canonical_name="D1")
    only_dup = Tenant.objects.create(provider="ocs", external_id="exp9", canonical_name="Exp9")
    canon_tm = TenantMembership.objects.create(user=canonical, tenant=shared)
    TenantCredential.objects.create(
        tenant_membership=canon_tm, credential_type=TenantCredential.OAUTH,
    )
    TenantMembership.objects.create(user=duplicate, tenant=shared)  # conflict
    TenantMembership.objects.create(user=duplicate, tenant=only_dup)

    report = merge_users(canonical=canonical, duplicate=duplicate)

    assert report.tenant_membership_repointed == 1
    assert report.tenant_membership_conflict_deleted == 1
    # canonical still has its row for the shared tenant
    assert TenantMembership.objects.filter(user=canonical, tenant=shared).count() == 1
    # canonical now also has only_dup's tenant
    assert TenantMembership.objects.filter(user=canonical, tenant=only_dup).exists()
```

- [ ] **Step 2: Run tests to verify they fail**

```
uv run pytest tests/test_merge_users_service.py -v
```

Expected: two new tests FAIL.

- [ ] **Step 3: Implement TenantMembership merge**

Add to `apps/users/services/merge.py` imports:

```python
from apps.users.models import TenantMembership
```

Add helper:

```python
def _merge_tenant_memberships(canonical: "User", duplicate: "User") -> tuple[int, int]:
    """Returns (repointed_count, conflict_deleted_count)."""
    canonical_tenant_ids = set(
        TenantMembership.objects.filter(user=canonical).values_list("tenant_id", flat=True)
    )
    dup_qs = TenantMembership.objects.filter(user=duplicate)
    conflicts = dup_qs.filter(tenant_id__in=canonical_tenant_ids)
    conflict_deleted, _ = conflicts.delete()  # TenantCredential cascades
    repointed = dup_qs.exclude(tenant_id__in=canonical_tenant_ids).update(user=canonical)
    return repointed, conflict_deleted
```

Call it in `merge_users`:

```python
        report.tenant_membership_repointed, report.tenant_membership_conflict_deleted = (
            _merge_tenant_memberships(canonical, duplicate)
        )
```

And update the `dry_run` branch similarly with `.count()` calls.

- [ ] **Step 4: Run tests to verify they pass**

```
uv run pytest tests/test_merge_users_service.py -v
```

Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/users/services/merge.py tests/test_merge_users_service.py
git commit -m "feat(users): merge TenantMembership with conflict resolution"
```

---

### Task 7: Merge WorkspaceMembership with role upgrade

**Files:**
- Modify: `apps/users/services/merge.py`
- Modify: `tests/test_merge_users_service.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_merge_users_service.py`:

```python
from apps.workspaces.models import Workspace, WorkspaceMembership, WorkspaceRole


@pytest.mark.django_db
def test_workspacemembership_repoints_when_no_overlap():
    canonical = User.objects.create(email="canon@y.com", username="canon")
    duplicate = User.objects.create(email="dup@y.com", username="dup")
    ws = Workspace.objects.create(name="W")
    WorkspaceMembership.objects.create(workspace=ws, user=duplicate, role=WorkspaceRole.READ)

    report = merge_users(canonical=canonical, duplicate=duplicate)

    assert report.workspace_membership_repointed == 1
    assert report.workspace_membership_conflict_merged == 0
    assert WorkspaceMembership.objects.get(workspace=ws).user == canonical


@pytest.mark.django_db
def test_workspacemembership_conflict_upgrades_to_higher_role():
    canonical = User.objects.create(email="canon@y.com", username="canon")
    duplicate = User.objects.create(email="dup@y.com", username="dup")
    ws = Workspace.objects.create(name="W")
    WorkspaceMembership.objects.create(workspace=ws, user=canonical, role=WorkspaceRole.READ)
    WorkspaceMembership.objects.create(workspace=ws, user=duplicate, role=WorkspaceRole.MANAGE)

    report = merge_users(canonical=canonical, duplicate=duplicate)

    assert report.workspace_membership_conflict_merged == 1
    membership = WorkspaceMembership.objects.get(workspace=ws)
    assert membership.user == canonical
    assert membership.role == WorkspaceRole.MANAGE


@pytest.mark.django_db
def test_workspacemembership_conflict_keeps_canonical_when_higher():
    canonical = User.objects.create(email="canon@y.com", username="canon")
    duplicate = User.objects.create(email="dup@y.com", username="dup")
    ws = Workspace.objects.create(name="W")
    WorkspaceMembership.objects.create(workspace=ws, user=canonical, role=WorkspaceRole.MANAGE)
    WorkspaceMembership.objects.create(workspace=ws, user=duplicate, role=WorkspaceRole.READ)

    merge_users(canonical=canonical, duplicate=duplicate)

    assert WorkspaceMembership.objects.get(workspace=ws).role == WorkspaceRole.MANAGE
```

- [ ] **Step 2: Run tests to verify they fail**

```
uv run pytest tests/test_merge_users_service.py -v
```

Expected: three new tests FAIL.

- [ ] **Step 3: Implement WorkspaceMembership merge**

Add to `apps/users/services/merge.py` imports:

```python
from apps.workspaces.models import WorkspaceMembership, WorkspaceRole
```

Add helper:

```python
_ROLE_RANK = {
    WorkspaceRole.READ: 0,
    WorkspaceRole.READ_WRITE: 1,
    WorkspaceRole.MANAGE: 2,
}


def _merge_workspace_memberships(canonical: "User", duplicate: "User") -> tuple[int, int]:
    """Returns (repointed_count, conflict_merged_count)."""
    canonical_by_ws = {
        m.workspace_id: m for m in WorkspaceMembership.objects.filter(user=canonical)
    }
    dup_memberships = list(WorkspaceMembership.objects.filter(user=duplicate))
    conflict_merged = 0
    repointed = 0
    for dup_m in dup_memberships:
        canon_m = canonical_by_ws.get(dup_m.workspace_id)
        if canon_m is None:
            dup_m.user = canonical
            dup_m.save(update_fields=["user"])
            repointed += 1
            continue
        if _ROLE_RANK[WorkspaceRole(dup_m.role)] > _ROLE_RANK[WorkspaceRole(canon_m.role)]:
            canon_m.role = dup_m.role
            canon_m.save(update_fields=["role"])
        dup_m.delete()
        conflict_merged += 1
    return repointed, conflict_merged
```

Call in `merge_users`:

```python
        report.workspace_membership_repointed, report.workspace_membership_conflict_merged = (
            _merge_workspace_memberships(canonical, duplicate)
        )
```

And add equivalent count-only logic in the `dry_run` branch.

- [ ] **Step 4: Run tests to verify they pass**

```
uv run pytest tests/test_merge_users_service.py -v
```

Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/users/services/merge.py tests/test_merge_users_service.py
git commit -m "feat(users): merge WorkspaceMembership with role-upgrade conflict resolution"
```

---

### Task 8: Long-tail FK repointing via introspection

**Files:**
- Modify: `apps/users/services/merge.py`
- Modify: `tests/test_merge_users_service.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_merge_users_service.py`:

```python
from apps.chat.models import Thread


@pytest.mark.django_db
def test_chat_threads_are_repointed_via_introspection():
    canonical = User.objects.create(email="canon@y.com", username="canon")
    duplicate = User.objects.create(email="dup@y.com", username="dup")
    Thread.objects.create(user=duplicate, title="T1")
    Thread.objects.create(user=duplicate, title="T2")

    report = merge_users(canonical=canonical, duplicate=duplicate)

    assert Thread.objects.filter(user=canonical).count() == 2
    assert Thread.objects.filter(user=duplicate).count() == 0
    label = "chat.Thread.user"
    assert report.long_tail_fk_counts.get(label) == 2


@pytest.mark.django_db
def test_setnull_fk_is_repointed_via_introspection():
    canonical = User.objects.create(email="canon@y.com", username="canon")
    duplicate = User.objects.create(email="dup@y.com", username="dup")
    ws = Workspace.objects.create(name="W", created_by=duplicate)

    merge_users(canonical=canonical, duplicate=duplicate)

    ws.refresh_from_db()
    assert ws.created_by == canonical
```

Note: `Thread` may require a workspace FK or other related fields. Adjust the test setup based on `apps/chat/models.py` — read the file before writing the test if `Thread.objects.create(user=...)` raises `IntegrityError` for missing required fields. Use `pytest -x` to short-circuit on the first failure and inspect.

- [ ] **Step 2: Run tests to verify they fail**

```
uv run pytest tests/test_merge_users_service.py -v
```

Expected: both new tests FAIL (no long-tail loop yet).

- [ ] **Step 3: Implement introspection loop**

In `apps/users/services/merge.py`, add at module level:

```python
_SPECIAL_CASE_MODELS = frozenset({
    "socialaccount.SocialAccount",
    "account.EmailAddress",
    "users.TenantMembership",
    "workspaces.WorkspaceMembership",
})


def _repoint_long_tail_fks(canonical: "User", duplicate: "User") -> dict[str, int]:
    """Bulk-update every User FK except those handled by special-case logic.

    Picks up new User FKs added by future apps without code changes. If a
    new FK has a unique constraint involving the user field, this raises
    IntegrityError on the bulk update — surface that and add explicit
    handling to merge_users.
    """
    counts: dict[str, int] = {}
    for rel in canonical._meta.related_objects:
        label = rel.related_model._meta.label
        if label in _SPECIAL_CASE_MODELS:
            continue
        field_name = rel.field.name
        n = rel.related_model._default_manager.filter(
            **{field_name: duplicate},
        ).update(**{field_name: canonical})
        if n:
            counts[f"{label}.{field_name}"] = n
    return counts
```

Call in `merge_users` after the workspace membership step:

```python
        report.long_tail_fk_counts = _repoint_long_tail_fks(canonical, duplicate)
```

Skip in the `dry_run` branch (counting per-field via introspection is verbose; the per-field detail isn't load-bearing for the plan preview).

- [ ] **Step 4: Run tests to verify they pass**

```
uv run pytest tests/test_merge_users_service.py -v
```

Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/users/services/merge.py tests/test_merge_users_service.py
git commit -m "feat(users): repoint long-tail User FKs via _meta.related_objects"
```

---

### Task 9: Delete the duplicate user + atomic rollback

**Files:**
- Modify: `apps/users/services/merge.py`
- Modify: `tests/test_merge_users_service.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_merge_users_service.py`:

```python
from unittest.mock import patch


@pytest.mark.django_db
def test_duplicate_user_row_is_deleted():
    canonical = User.objects.create(email="canon@y.com", username="canon")
    duplicate = User.objects.create(email="dup@y.com", username="dup")

    report = merge_users(canonical=canonical, duplicate=duplicate)

    assert report.duplicate_user_deleted is True
    assert not User.objects.filter(pk=duplicate.pk).exists()
    assert User.objects.filter(pk=canonical.pk).exists()


@pytest.mark.django_db
def test_merge_rolls_back_on_exception():
    canonical = User.objects.create(email="canon@y.com", username="canon")
    duplicate = User.objects.create(email="dup@y.com", username="dup")
    SocialAccount.objects.create(user=duplicate, provider="commcare", uid="42")

    with patch(
        "apps.users.services.merge._repoint_long_tail_fks",
        side_effect=RuntimeError("simulated failure"),
    ), pytest.raises(RuntimeError):
        merge_users(canonical=canonical, duplicate=duplicate)

    # Everything must be untouched
    assert User.objects.filter(pk=duplicate.pk).exists()
    assert SocialAccount.objects.get(provider="commcare", uid="42").user == duplicate
```

- [ ] **Step 2: Run tests to verify they fail**

```
uv run pytest tests/test_merge_users_service.py -v
```

Expected: both new tests FAIL.

- [ ] **Step 3: Implement deletion + finalize atomic wrapping**

Update `merge_users` body. Full state of the function should now be:

```python
def merge_users(*, canonical, duplicate, dry_run=False):
    if canonical.pk == duplicate.pk:
        raise ValueError("canonical and duplicate must be different users")
    report = MergeReport(
        canonical_id=canonical.pk, duplicate_id=duplicate.pk, dry_run=dry_run,
    )
    if dry_run:
        # Count-only plan (no writes).
        report.socialaccount_repointed = SocialAccount.objects.filter(user=duplicate).count()
        canonical_emails = set(
            EmailAddress.objects.filter(user=canonical).values_list("email", flat=True)
        )
        dup_emails = EmailAddress.objects.filter(user=duplicate)
        report.emailaddress_deleted = dup_emails.filter(email__in=canonical_emails).count()
        report.emailaddress_repointed = dup_emails.exclude(email__in=canonical_emails).count()
        canonical_tenant_ids = set(
            TenantMembership.objects.filter(user=canonical).values_list("tenant_id", flat=True)
        )
        dup_tms = TenantMembership.objects.filter(user=duplicate)
        report.tenant_membership_conflict_deleted = dup_tms.filter(
            tenant_id__in=canonical_tenant_ids,
        ).count()
        report.tenant_membership_repointed = dup_tms.exclude(
            tenant_id__in=canonical_tenant_ids,
        ).count()
        canonical_ws_ids = set(
            WorkspaceMembership.objects.filter(user=canonical).values_list(
                "workspace_id", flat=True,
            )
        )
        dup_wms = WorkspaceMembership.objects.filter(user=duplicate)
        report.workspace_membership_conflict_merged = dup_wms.filter(
            workspace_id__in=canonical_ws_ids,
        ).count()
        report.workspace_membership_repointed = dup_wms.exclude(
            workspace_id__in=canonical_ws_ids,
        ).count()
        return report

    with transaction.atomic():
        report.field_changes = _merge_user_fields(canonical, duplicate)
        report.socialaccount_repointed = _repoint_social_accounts(canonical, duplicate)
        report.emailaddress_repointed, report.emailaddress_deleted = (
            _dedupe_email_addresses(canonical, duplicate)
        )
        report.tenant_membership_repointed, report.tenant_membership_conflict_deleted = (
            _merge_tenant_memberships(canonical, duplicate)
        )
        report.workspace_membership_repointed, report.workspace_membership_conflict_merged = (
            _merge_workspace_memberships(canonical, duplicate)
        )
        report.long_tail_fk_counts = _repoint_long_tail_fks(canonical, duplicate)
        duplicate.delete()
        report.duplicate_user_deleted = True
    logger.info(
        "Merged user=%s into canonical=%s: %s",
        report.duplicate_id, report.canonical_id, report,
    )
    return report
```

- [ ] **Step 4: Run tests to verify they pass**

```
uv run pytest tests/test_merge_users_service.py -v
```

Expected: all tests (~16) PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/users/services/merge.py tests/test_merge_users_service.py
git commit -m "feat(users): finalize merge — delete duplicate, atomic rollback on failure"
```

---

### Task 10: Dry-run preserves DB state

**Files:**
- Modify: `tests/test_merge_users_service.py`

This task is verification-only — the dry-run code path was added in Task 4 and refined through Task 9. We pin it down with a dedicated test.

- [ ] **Step 1: Write the test**

Append to `tests/test_merge_users_service.py`:

```python
@pytest.mark.django_db
def test_dry_run_writes_nothing_and_returns_a_plan():
    canonical = User.objects.create(email="canon@y.com", username="canon")
    duplicate = User.objects.create(email="dup@y.com", username="dup")
    SocialAccount.objects.create(user=duplicate, provider="commcare", uid="42")
    EmailAddress.objects.create(user=duplicate, email="canon@y.com", primary=True)

    report = merge_users(canonical=canonical, duplicate=duplicate, dry_run=True)

    assert report.dry_run is True
    assert report.socialaccount_repointed == 1
    assert report.emailaddress_deleted == 1
    # Nothing actually changed
    assert SocialAccount.objects.get(provider="commcare", uid="42").user == duplicate
    assert User.objects.filter(pk=duplicate.pk).exists()
    assert not report.duplicate_user_deleted
```

- [ ] **Step 2: Run test to verify it passes**

```
uv run pytest tests/test_merge_users_service.py::test_dry_run_writes_nothing_and_returns_a_plan -v
```

Expected: PASS (dry-run already implemented; this is the regression pin).

- [ ] **Step 3: Commit**

```bash
git add tests/test_merge_users_service.py
git commit -m "test(users): pin merge_users dry-run leaves DB untouched"
```

---

## Phase 3 — Signal handler

### Task 11: Handler skeleton + no-op branches

**Files:**
- Modify: `apps/users/signals.py`
- Create: `tests/test_social_login_reconciliation.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_social_login_reconciliation.py`:

```python
"""Tests for the pre_social_login handler that backfills/merges emails."""

from types import SimpleNamespace

import pytest
from django.contrib.auth import get_user_model

from apps.users.signals import reconcile_existing_user_on_login

User = get_user_model()


def _sociallogin(user, extra_data):
    """Build a SocialLogin-shaped stub. Handler only touches .user and .account."""
    return SimpleNamespace(
        user=user,
        account=SimpleNamespace(extra_data=extra_data, user=user),
    )


@pytest.mark.django_db
def test_brand_new_user_is_noop():
    new_user = User(email=None)  # unsaved → pk is None
    sl = _sociallogin(new_user, {"email": "x@y.com"})

    reconcile_existing_user_on_login(sender=None, request=None, sociallogin=sl)

    # No DB writes; new_user remains unsaved
    assert new_user.pk is None


@pytest.mark.django_db
def test_existing_user_with_email_is_noop():
    existing = User.objects.create(email="brian@y.com", username="b")
    sl = _sociallogin(existing, {"email": "other@y.com"})

    reconcile_existing_user_on_login(sender=None, request=None, sociallogin=sl)

    existing.refresh_from_db()
    assert existing.email == "brian@y.com"  # untouched


@pytest.mark.django_db
def test_no_email_in_extra_data_is_noop():
    existing = User.objects.create(email=None, username="b")
    sl = _sociallogin(existing, {})  # no email key

    reconcile_existing_user_on_login(sender=None, request=None, sociallogin=sl)

    existing.refresh_from_db()
    assert existing.email is None
```

- [ ] **Step 2: Run tests to verify they fail**

```
uv run pytest tests/test_social_login_reconciliation.py -v
```

Expected: ImportError — `reconcile_existing_user_on_login` doesn't exist yet.

- [ ] **Step 3: Implement handler skeleton**

Edit `apps/users/signals.py`. Add to existing imports (module level):

```python
from allauth.socialaccount.signals import pre_social_login
from django.contrib.auth import get_user_model

from apps.users.services.merge import merge_users
```

Append at module level (below existing `resolve_tenant_on_social_login`):

```python
def reconcile_existing_user_on_login(sender, request, sociallogin, **kwargs):
    """Bridge the gap where allauth's _lookup_by_socialaccount short-circuits.

    When an existing OAuth user logs in and the provider now returns an email
    that the User row doesn't yet have, either backfill it or merge into the
    user that already owns that email.

    Implementation arrives across this phase's tasks.
    """
    new_email = sociallogin.account.extra_data.get("email")
    if not new_email:
        return
    user = sociallogin.user
    if user.pk is None:
        return  # brand-new user; allauth's _lookup_by_email already handled it
    if user.email:
        return  # already has an email; nothing to reconcile
```

- [ ] **Step 4: Run tests to verify they pass**

```
uv run pytest tests/test_social_login_reconciliation.py -v
```

Expected: all 3 PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/users/signals.py tests/test_social_login_reconciliation.py
git commit -m "feat(users): scaffold pre_social_login handler with no-op branches"
```

---

### Task 12: Backfill email when no collision

**Files:**
- Modify: `apps/users/signals.py`
- Modify: `tests/test_social_login_reconciliation.py`

- [ ] **Step 1: Write failing test**

Append to `tests/test_social_login_reconciliation.py`:

```python
@pytest.mark.django_db
def test_no_collision_backfills_user_email():
    existing = User.objects.create(email=None, username="connect-user")
    sl = _sociallogin(existing, {"email": "brian@y.com"})

    reconcile_existing_user_on_login(sender=None, request=None, sociallogin=sl)

    existing.refresh_from_db()
    assert existing.email == "brian@y.com"
```

(Case-insensitive collision behavior is tested in Task 13 once the merge branch lands.)

- [ ] **Step 2: Run test to verify it fails**

```
uv run pytest tests/test_social_login_reconciliation.py -v
```

Expected: `test_no_collision_backfills_user_email` FAILS (email stays None).

- [ ] **Step 3: Implement backfill branch**

In `apps/users/signals.py`, extend `reconcile_existing_user_on_login`:

```python
def reconcile_existing_user_on_login(sender, request, sociallogin, **kwargs):
    new_email = sociallogin.account.extra_data.get("email")
    if not new_email:
        return
    user = sociallogin.user
    if user.pk is None:
        return
    if user.email:
        return

    UserModel = get_user_model()
    canonical = (
        UserModel.objects.filter(email__iexact=new_email).exclude(pk=user.pk).first()
    )
    if canonical is None:
        user.email = new_email
        user.save(update_fields=["email"])
        return
    # Collision branch lives in task 13.
```

- [ ] **Step 4: Run tests to verify they pass**

```
uv run pytest tests/test_social_login_reconciliation.py -v
```

Expected: all 5 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/users/signals.py tests/test_social_login_reconciliation.py
git commit -m "feat(users): backfill User.email on OAuth login when no collision"
```

---

### Task 13: Trigger merge on collision and redirect session

**Files:**
- Modify: `apps/users/signals.py`
- Modify: `tests/test_social_login_reconciliation.py`

- [ ] **Step 1: Write failing test**

Append to `tests/test_social_login_reconciliation.py`:

```python
from allauth.socialaccount.models import SocialAccount


@pytest.mark.django_db
def test_collision_merges_user_and_redirects_session():
    canonical = User.objects.create(email="brian@y.com", username="canon")
    duplicate = User.objects.create(email=None, username="connect-user")
    dup_account = SocialAccount.objects.create(
        user=duplicate, provider="commcare_connect", uid="999",
        extra_data={"email": "brian@y.com"},
    )
    sl = SimpleNamespace(user=duplicate, account=dup_account)

    reconcile_existing_user_on_login(sender=None, request=None, sociallogin=sl)

    # duplicate was merged away
    assert not User.objects.filter(pk=duplicate.pk).exists()
    # Connect SocialAccount now points at canonical
    dup_account.refresh_from_db()
    assert dup_account.user == canonical
    # Session redirected
    assert sl.user == canonical
    assert sl.account.user == canonical


@pytest.mark.django_db
def test_collision_match_is_case_insensitive():
    canonical = User.objects.create(email="Brian@Y.com", username="canon")
    duplicate = User.objects.create(email=None, username="dup")
    dup_account = SocialAccount.objects.create(
        user=duplicate, provider="commcare_connect", uid="x",
        extra_data={"email": "brian@y.com"},
    )
    sl = SimpleNamespace(user=duplicate, account=dup_account)

    reconcile_existing_user_on_login(sender=None, request=None, sociallogin=sl)

    assert not User.objects.filter(pk=duplicate.pk).exists()
    assert sl.user == canonical
```

`SocialAccount` has a real `extra_data` JSONField, so we set the email via the model's `extra_data=` kwarg rather than stubbing a `SimpleNamespace`.

- [ ] **Step 2: Run test to verify it fails**

```
uv run pytest tests/test_social_login_reconciliation.py::test_collision_merges_user_and_redirects_session -v
```

Expected: FAIL — duplicate not deleted, account not repointed.

- [ ] **Step 3: Implement collision branch**

Replace the final lines of `reconcile_existing_user_on_login` in `apps/users/signals.py`:

```python
    if canonical is None:
        user.email = new_email
        user.save(update_fields=["email"])
        return

    merge_users(canonical=canonical, duplicate=user)
    sociallogin.user = canonical
    sociallogin.account.user = canonical
    logger.info(
        "auto-merge: user=%s into canonical=%s email=%s provider=%s",
        user.pk, canonical.pk, new_email, sociallogin.account.provider,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

```
uv run pytest tests/test_social_login_reconciliation.py -v
```

Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/users/signals.py tests/test_social_login_reconciliation.py
git commit -m "feat(users): auto-merge on collision and redirect login session"
```

---

### Task 14: Merge failure must not block login

**Files:**
- Modify: `apps/users/signals.py`
- Modify: `tests/test_social_login_reconciliation.py`

- [ ] **Step 1: Write failing test**

Append to `tests/test_social_login_reconciliation.py`:

```python
from unittest.mock import patch


@pytest.mark.django_db
def test_merge_failure_does_not_break_login(caplog):
    canonical = User.objects.create(email="brian@y.com", username="canon")
    duplicate = User.objects.create(email=None, username="connect-user")
    dup_account = SocialAccount.objects.create(
        user=duplicate, provider="commcare_connect", uid="999",
        extra_data={"email": "brian@y.com"},
    )
    sl = SimpleNamespace(user=duplicate, account=dup_account)

    with patch(
        "apps.users.signals.merge_users",
        side_effect=RuntimeError("boom"),
    ):
        # Must not raise.
        reconcile_existing_user_on_login(sender=None, request=None, sociallogin=sl)

    # Duplicate still present, login continues on duplicate
    assert User.objects.filter(pk=duplicate.pk).exists()
    assert sl.user == duplicate
    # Failure was logged at ERROR
    assert any(
        r.levelname == "ERROR" and "Auto-merge failed" in r.message
        for r in caplog.records
    )
```

- [ ] **Step 2: Run test to verify it fails**

```
uv run pytest tests/test_social_login_reconciliation.py::test_merge_failure_does_not_break_login -v
```

Expected: FAIL — `RuntimeError` propagates out of the handler.

- [ ] **Step 3: Wrap merge in try/except**

In `apps/users/signals.py`, replace the collision branch:

```python
    try:
        merge_users(canonical=canonical, duplicate=user)
    except Exception:
        logger.exception(
            "Auto-merge failed for user=%s into canonical=%s", user.pk, canonical.pk,
        )
        return
    sociallogin.user = canonical
    sociallogin.account.user = canonical
    logger.info(
        "auto-merge: user=%s into canonical=%s email=%s provider=%s",
        user.pk, canonical.pk, new_email, sociallogin.account.provider,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

```
uv run pytest tests/test_social_login_reconciliation.py -v
```

Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/users/signals.py tests/test_social_login_reconciliation.py
git commit -m "feat(users): swallow merge failure during login so users can still sign in"
```

---

### Task 15: Wire the signal in apps.py

**Files:**
- Modify: `apps/users/apps.py`
- Modify: `tests/test_social_login_reconciliation.py` (add wiring assertion)

- [ ] **Step 1: Write failing test**

Append to `tests/test_social_login_reconciliation.py`:

```python
def test_signal_is_wired_in_app_ready():
    from allauth.socialaccount.signals import pre_social_login

    receivers = [
        ref() for _, ref in pre_social_login.receivers if ref() is not None
    ]
    receiver_names = [getattr(r, "__name__", "") for r in receivers]
    assert "reconcile_existing_user_on_login" in receiver_names
```

- [ ] **Step 2: Run test to verify it fails**

```
uv run pytest tests/test_social_login_reconciliation.py::test_signal_is_wired_in_app_ready -v
```

Expected: FAIL — handler isn't connected yet.

- [ ] **Step 3: Wire the signal in apps.py**

Edit `apps/users/apps.py`:

```python
from django.apps import AppConfig


class UsersConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.users"
    verbose_name = "Users"

    def ready(self):
        from allauth.socialaccount.signals import (
            pre_social_login,
            social_account_added,
            social_account_updated,
        )

        import apps.users.signals  # noqa: F401 — connects auto_create_workspace_on_membership
        from apps.users.signals import (
            reconcile_existing_user_on_login,
            resolve_tenant_on_social_login,
        )

        social_account_added.connect(resolve_tenant_on_social_login)
        social_account_updated.connect(resolve_tenant_on_social_login)
        pre_social_login.connect(reconcile_existing_user_on_login)
```

- [ ] **Step 4: Run tests to verify they pass**

```
uv run pytest tests/test_social_login_reconciliation.py -v
```

Expected: all tests PASS.

Also run the full test suite to make sure nothing else broke:

```
uv run pytest -q
```

Expected: green.

- [ ] **Step 5: Commit**

```bash
git add apps/users/apps.py tests/test_social_login_reconciliation.py
git commit -m "feat(users): wire pre_social_login handler in UsersConfig.ready"
```

---

## Phase 4 — Management command

### Task 16: Command skeleton — find duplicate groups + canonical selection

**Files:**
- Create: `apps/users/management/commands/merge_duplicate_users.py`
- Create: `tests/test_merge_duplicate_users_command.py`

- [ ] **Step 1: Write failing test**

`User.email` has `unique=True`, enforced at the DB level. We can't directly insert two rows with the same email — but the model's unique constraint is case-sensitive in PostgreSQL, so `brian@y.com` and `Brian@Y.com` legally coexist. The command groups case-insensitively (per spec), so these count as one duplicate group. Tests use case variations to construct the duplicate state.

Create `tests/test_merge_duplicate_users_command.py`:

```python
"""Tests for the merge_duplicate_users management command."""

from io import StringIO

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command

User = get_user_model()


@pytest.mark.django_db
def test_dry_run_finds_no_duplicates_when_emails_are_unique():
    User.objects.create(email="a@y.com", username="a")
    User.objects.create(email="b@y.com", username="b")
    out = StringIO()

    call_command("merge_duplicate_users", "--dry-run", stdout=out)

    assert "no duplicates found" in out.getvalue().lower()


@pytest.mark.django_db
def test_dry_run_lists_duplicate_groups():
    older = User.objects.create(email="brian@y.com", username="older")
    older.set_password("pw")
    older.save()
    newer = User.objects.create(email="Brian@Y.com", username="newer")
    out = StringIO()

    call_command("merge_duplicate_users", "--dry-run", stdout=out)

    output = out.getvalue()
    assert "brian@y.com" in output.lower()
    assert f"canonical: User#{older.pk}" in output
    # dry-run leaves DB intact
    assert User.objects.filter(pk=newer.pk).exists()
```

- [ ] **Step 2: Run tests to verify they fail**

```
uv run pytest tests/test_merge_duplicate_users_command.py -v
```

Expected: FAIL — command doesn't exist.

- [ ] **Step 3: Implement command skeleton**

Create `apps/users/management/commands/merge_duplicate_users.py`:

```python
"""Operator command to merge duplicate User rows that share an email.

Usage:
    python manage.py merge_duplicate_users [--dry-run] [--email EMAIL]
                                           [--canonical-id ID] [--yes]
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db.models.functions import Lower

from apps.users.services.merge import MergeReport, merge_users, select_canonical

User = get_user_model()


class Command(BaseCommand):
    help = "Merge duplicate User rows that share an email address."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--dry-run", action="store_true", help="Print plan, write nothing.")
        parser.add_argument("--email", help="Only operate on the group sharing this email.")
        parser.add_argument(
            "--canonical-id", type=int,
            help="Force this user as canonical. Must be in the targeted group.",
        )
        parser.add_argument("--yes", action="store_true", help="Skip the confirmation prompt.")

    def handle(self, *args: Any, **opts: Any) -> None:
        groups = self._find_groups(target_email=opts.get("email"))
        if not groups:
            target = opts.get("email")
            self.stdout.write(
                f"no duplicates found{' for ' + target if target else ''}"
            )
            return

        plans: list[tuple[list[User], MergeReport, User]] = []
        for users in groups:
            canonical = self._pick_canonical(users, forced_id=opts.get("canonical_id"))
            for dup in users:
                if dup.pk == canonical.pk:
                    continue
                report = merge_users(canonical=canonical, duplicate=dup, dry_run=True)
                plans.append(([canonical, dup], report, canonical))
                self._print_plan(canonical, dup, report)

        if opts.get("dry_run"):
            return

        if not opts.get("yes"):
            response = input(
                f"About to merge {len(plans)} duplicate(s). Continue? [y/N] "
            ).strip().lower()
            if response != "y":
                self.stdout.write("aborted")
                return

        for users, _plan, canonical in plans:
            dup = next(u for u in users if u.pk != canonical.pk)
            try:
                merge_users(canonical=canonical, duplicate=dup)
                self.stdout.write(f"merged User#{dup.pk} → User#{canonical.pk}")
            except Exception as exc:  # noqa: BLE001 — best-effort per-group recovery
                self.stderr.write(f"failed User#{dup.pk} → User#{canonical.pk}: {exc!r}")

    def _find_groups(self, *, target_email: str | None) -> list[list[User]]:
        qs = User.objects.exclude(email__isnull=True).exclude(email="")
        if target_email:
            qs = qs.filter(email__iexact=target_email)
        buckets: dict[str, list[User]] = defaultdict(list)
        for u in qs.annotate(lower_email=Lower("email")).order_by("created_at", "pk"):
            buckets[u.lower_email].append(u)
        return [group for group in buckets.values() if len(group) > 1]

    def _pick_canonical(self, users: list[User], *, forced_id: int | None) -> User:
        if forced_id is not None:
            forced = next((u for u in users if u.pk == forced_id), None)
            if forced is None:
                raise CommandError(
                    f"--canonical-id={forced_id} is not in the targeted duplicate group",
                )
            return forced
        return select_canonical(users)

    def _print_plan(self, canonical: User, duplicate: User, report: MergeReport) -> None:
        self.stdout.write(
            f"[merge] email='{canonical.email}'  canonical: User#{canonical.pk}  "
            f"duplicate: User#{duplicate.pk}"
        )
        self.stdout.write(
            f"  plan: socialaccounts={report.socialaccount_repointed} "
            f"emails(repoint/delete)={report.emailaddress_repointed}/"
            f"{report.emailaddress_deleted} "
            f"tenant(repoint/conflict)={report.tenant_membership_repointed}/"
            f"{report.tenant_membership_conflict_deleted} "
            f"workspace(repoint/conflict)={report.workspace_membership_repointed}/"
            f"{report.workspace_membership_conflict_merged}"
        )
```

- [ ] **Step 4: Run tests to verify they pass**

```
uv run pytest tests/test_merge_duplicate_users_command.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/users/management/commands/merge_duplicate_users.py tests/test_merge_duplicate_users_command.py
git commit -m "feat(users): add merge_duplicate_users management command (dry-run)"
```

---

### Task 17: --email and --canonical-id targeting

**Files:**
- Modify: `tests/test_merge_duplicate_users_command.py`

- [ ] **Step 1: Write failing tests**

Append:

```python
@pytest.mark.django_db
def test_email_flag_targets_only_one_group():
    User.objects.create(email="x@y.com", username="x1")
    User.objects.create(email="X@y.com", username="x2")
    User.objects.create(email="a@y.com", username="a1")
    User.objects.create(email="A@y.com", username="a2")
    out = StringIO()

    call_command("merge_duplicate_users", "--dry-run", "--email", "x@y.com", stdout=out)

    output = out.getvalue()
    assert "x@y.com" in output.lower()
    assert "a@y.com" not in output.lower()


@pytest.mark.django_db
def test_email_flag_with_no_duplicates_exits_gracefully():
    User.objects.create(email="only@y.com", username="only")
    out = StringIO()

    call_command("merge_duplicate_users", "--dry-run", "--email", "only@y.com", stdout=out)

    assert "no duplicates found" in out.getvalue().lower()


@pytest.mark.django_db
def test_canonical_id_forces_canonical_choice():
    from django.core.management import CommandError as _CE

    older = User.objects.create(email="x@y.com", username="x1")
    older.set_password("pw")
    older.save()
    newer = User.objects.create(email="X@y.com", username="x2")
    out = StringIO()

    call_command(
        "merge_duplicate_users", "--dry-run", "--email", "x@y.com",
        "--canonical-id", str(newer.pk), stdout=out,
    )

    output = out.getvalue()
    assert f"canonical: User#{newer.pk}" in output

    # Invalid canonical-id raises
    with pytest.raises(_CE):
        call_command(
            "merge_duplicate_users", "--dry-run", "--email", "x@y.com",
            "--canonical-id", "999999", stdout=out,
        )
```

- [ ] **Step 2: Run tests to verify they fail or pass**

```
uv run pytest tests/test_merge_duplicate_users_command.py -v
```

Expected: all PASS (the command already supports both flags from Task 16).

- [ ] **Step 3: Commit**

```bash
git add tests/test_merge_duplicate_users_command.py
git commit -m "test(users): pin --email and --canonical-id behavior"
```

---

### Task 18: Confirmation prompt + --yes + real execution

**Files:**
- Modify: `tests/test_merge_duplicate_users_command.py`

- [ ] **Step 1: Write failing tests**

Append:

```python
from unittest.mock import patch


@pytest.mark.django_db
def test_yes_flag_skips_prompt_and_executes_merge():
    older = User.objects.create(email="x@y.com", username="x1")
    older.set_password("pw")
    older.save()
    newer = User.objects.create(email="X@y.com", username="x2")
    out = StringIO()

    call_command("merge_duplicate_users", "--yes", stdout=out)

    assert not User.objects.filter(pk=newer.pk).exists()
    assert User.objects.filter(pk=older.pk).exists()
    assert f"merged User#{newer.pk}" in out.getvalue()


@pytest.mark.django_db
def test_prompt_rejection_aborts_without_changes():
    older = User.objects.create(email="x@y.com", username="x1")
    older.set_password("pw")
    older.save()
    newer = User.objects.create(email="X@y.com", username="x2")
    out = StringIO()

    with patch("builtins.input", return_value="n"):
        call_command("merge_duplicate_users", stdout=out)

    assert User.objects.filter(pk=newer.pk).exists()
    assert "aborted" in out.getvalue().lower()


@pytest.mark.django_db
def test_failure_in_one_group_does_not_block_others():
    # Group A — will fail
    a1 = User.objects.create(email="a@y.com", username="a1")
    a2 = User.objects.create(email="A@y.com", username="a2")
    # Group B — will succeed
    b1 = User.objects.create(email="b@y.com", username="b1")
    b1.set_password("pw")
    b1.save()
    b2 = User.objects.create(email="B@y.com", username="b2")
    out = StringIO()
    err = StringIO()

    call_count = {"n": 0}

    def fake_merge(*, canonical, duplicate, dry_run=False):
        if dry_run:
            return MergeReport(canonical_id=canonical.pk, duplicate_id=duplicate.pk, dry_run=True)
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("simulated failure on first group")
        # Real merge for second group
        from apps.users.services.merge import merge_users as real_merge
        return real_merge(canonical=canonical, duplicate=duplicate)

    with patch("apps.users.management.commands.merge_duplicate_users.merge_users", side_effect=fake_merge):
        call_command("merge_duplicate_users", "--yes", stdout=out, stderr=err)

    assert "failed" in err.getvalue().lower()
    # Second group still merged
    assert not User.objects.filter(pk=b2.pk).exists()
```

- [ ] **Step 2: Run tests to verify they fail**

```
uv run pytest tests/test_merge_duplicate_users_command.py -v
```

Expected: PASS if the command was implemented per Task 16 (it already includes prompt + try/except). If the failure-isolation test fails, ensure the command catches per-group exceptions and continues.

- [ ] **Step 3: If needed, refine the command**

Confirm the body of the execution loop in `merge_duplicate_users.py` matches:

```python
        for users, _plan, canonical in plans:
            dup = next(u for u in users if u.pk != canonical.pk)
            try:
                merge_users(canonical=canonical, duplicate=dup)
                self.stdout.write(f"merged User#{dup.pk} → User#{canonical.pk}")
            except Exception as exc:  # noqa: BLE001
                self.stderr.write(f"failed User#{dup.pk} → User#{canonical.pk}: {exc!r}")
```

- [ ] **Step 4: Run tests**

```
uv run pytest tests/test_merge_duplicate_users_command.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/users/management/commands/merge_duplicate_users.py tests/test_merge_duplicate_users_command.py
git commit -m "test(users): pin prompt/--yes behavior and per-group failure isolation"
```

---

## Phase 5 — Final verification

### Task 19: Full-suite green + lint

- [ ] **Step 1: Run all tests**

```
uv run pytest -q
```

Expected: green. If any pre-existing tests now fail, investigate — most likely a test relied on the absence of `SOCIALACCOUNT_EMAIL_AUTHENTICATION`, or on a provider's `VERIFIED_EMAIL` being False (e.g., asserts that an OAuth login created a new user when an email match was possible). Update those tests to match the new contract.

- [ ] **Step 2: Run ruff**

```
uv run ruff check apps/users tests/test_merge_users_service.py tests/test_social_login_reconciliation.py tests/test_merge_duplicate_users_command.py tests/test_auth_settings.py
uv run ruff format apps/users tests/test_merge_users_service.py tests/test_social_login_reconciliation.py tests/test_merge_duplicate_users_command.py tests/test_auth_settings.py
```

Expected: no lint errors. Fix anything reported (commonly: import ordering, line length).

- [ ] **Step 3: Commit any formatting fixes**

```bash
git add -p
git commit -m "chore: ruff formatting on new modules"
```

(Skip the commit if ruff made no changes.)

---

### Task 20: PR-ready end-to-end check

- [ ] **Step 1: Manually verify the dry-run command works on a clean DB**

```
docker compose up platform-db -d
uv run python manage.py migrate
uv run python manage.py merge_duplicate_users --dry-run
```

Expected output:

```
no duplicates found
```

- [ ] **Step 2: Create a synthetic duplicate and re-run**

In a Django shell (`uv run python manage.py shell`):

```python
from django.contrib.auth import get_user_model
U = get_user_model()
a = U.objects.create(email="dup@example.com", username="a"); a.set_password("x"); a.save()
b = U.objects.create(email="Dup@Example.com", username="b")
```

Then:

```
uv run python manage.py merge_duplicate_users --dry-run --email dup@example.com
```

Expected: prints the plan with canonical = User#`a.pk`. No DB changes.

```
uv run python manage.py merge_duplicate_users --yes --email dup@example.com
```

Expected: deletes User#`b.pk`, prints `merged ...`. Re-running shows `no duplicates found`.

- [ ] **Step 3: Confirm with git status that no stray files were created**

```
git status
```

Expected: clean.

- [ ] **Step 4: Final commit message review**

```
git log --oneline origin/main..HEAD
```

Expected: ~18-22 small commits, each describing one capability. PR can be opened as-is.

---

## Notes for the executing engineer

- **Adapter `pre_social_login` already exists.** `apps/users/adapters.py:73` (`EncryptingSocialAccountAdapter.pre_social_login`) implements OAuth domain restriction via `SOCIALACCOUNT_ALLOWED_EMAIL_DOMAINS`. allauth fires the adapter method **first** (`allauth/socialaccount/internal/flows/login.py:35`), then sends the `pre_social_login` signal. Domain-blocked logins raise `ImmediateHttpResponse` and our signal handler never runs — no conflict. Don't move our logic into the adapter; the adapter is already doing two things (token encryption + domain restriction), and the signal path keeps separation of concerns.
- **Imports at module level.** This codebase enforces no-inline-imports (per `CLAUDE.md`). The signal handler imports `merge_users` at module top — if you find yourself reaching for an inline import to dodge a circular dependency, stop and restructure instead.
- **Async vs sync.** All code in this plan is sync. The merge service uses `transaction.atomic()` which is sync-only; the signal handler also runs synchronously (allauth's signals are sync). Don't async-ify any of it.
- **ruff.** Line length is 100. Watch the long log/format strings — use parenthesized concatenation if needed.
- **Tests use `pytest-django` patterns.** `@pytest.mark.django_db` for any test that hits the DB; `transaction=True` only when the test exercises async ORM, which none of these do.
- **The merge service is the heart.** Spend extra care on Tasks 4–9 — every other piece depends on `merge_users` being correct.

## Spec coverage check

| Spec section | Tasks |
|---|---|
| A. Settings change | Task 1 |
| B. `pre_social_login` handler | Tasks 11–15 |
| C. Merge service (field merge) | Task 3 |
| C. Merge service (SocialAccount) | Task 4 |
| C. Merge service (EmailAddress) | Task 5 |
| C. Merge service (TenantMembership) | Task 6 |
| C. Merge service (WorkspaceMembership) | Task 7 |
| C. Merge service (long-tail introspection) | Task 8 |
| C. Merge service (delete + atomic) | Task 9 |
| C. Merge service (dry-run) | Task 10 |
| D. Merge command | Tasks 16–18 |
| Trust model | (Settings comment in Task 1) |
| Edge cases — case-insensitive | Task 12 |
| Edge cases — merge failure | Tasks 9, 14, 18 |
| Testing — service file | Tasks 2–10 |
| Testing — handler file | Tasks 11–15 |
| Rollout — dry-run + targeted | Task 20 |
| Observability — INFO log on auto-merge | Task 13 |
| Observability — ERROR log on failure | Task 14 |
