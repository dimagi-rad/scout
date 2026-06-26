"""Tests for the merge_duplicate_users management command."""

from io import StringIO
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command

from apps.users.services.merge import MergeReport
from apps.users.services.merge import merge_users as _merge_users_real

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
    older = User.objects.create(email="x@y.com", username="x1")
    older.set_password("pw")
    older.save()
    newer = User.objects.create(email="X@y.com", username="x2")
    out = StringIO()

    call_command(
        "merge_duplicate_users",
        "--dry-run",
        "--email",
        "x@y.com",
        "--canonical-id",
        str(newer.pk),
        stdout=out,
    )

    output = out.getvalue()
    assert f"canonical: User#{newer.pk}" in output

    # Invalid canonical-id raises
    with pytest.raises(CommandError):
        call_command(
            "merge_duplicate_users",
            "--dry-run",
            "--email",
            "x@y.com",
            "--canonical-id",
            "999999",
            stdout=out,
        )


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
def test_dry_run_surfaces_discarded_privilege_at_prompt():
    """11#4: when the duplicate carries privilege flags the canonical lacks, the
    printed plan must WARN that a privileged duplicate is being discarded (and
    that the flags are not propagated) — making the escalation risk visible
    before the y/N confirmation."""
    # Canonical is the older, password-holding row (selected as canonical).
    older = User.objects.create(email="x@y.com", username="x1")
    older.set_password("pw")
    older.save()
    # Duplicate is a stale createsuperuser-style artifact.
    User.objects.create(email="X@y.com", username="x2", is_staff=True, is_superuser=True)
    out = StringIO()

    call_command("merge_duplicate_users", "--dry-run", stdout=out)

    output = out.getvalue()
    assert "WARNING" in output
    assert "is_staff" in output
    assert "is_superuser" in output
    assert "not propagated" in output.lower()


@pytest.mark.django_db
def test_yes_flag_merge_does_not_escalate_canonical_privileges():
    """11#4 end-to-end: merging a privileged duplicate via the command must not
    promote the canonical to staff/superuser."""
    older = User.objects.create(email="x@y.com", username="x1")
    older.set_password("pw")
    older.save()
    newer = User.objects.create(email="X@y.com", username="x2", is_staff=True, is_superuser=True)
    out = StringIO()

    call_command("merge_duplicate_users", "--yes", stdout=out)

    older.refresh_from_db()
    assert older.is_staff is False
    assert older.is_superuser is False
    assert not User.objects.filter(pk=newer.pk).exists()


@pytest.mark.django_db
def test_failure_in_one_group_does_not_block_others():
    # Group A — will fail
    User.objects.create(email="a@y.com", username="a1")
    User.objects.create(email="A@y.com", username="a2")
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
            return MergeReport(
                canonical_id=canonical.pk,
                duplicate_id=duplicate.pk,
                dry_run=True,
            )
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("simulated failure on first group")
        # Real merge for second group
        return _merge_users_real(canonical=canonical, duplicate=duplicate)

    with patch(
        "apps.users.management.commands.merge_duplicate_users.merge_users",
        side_effect=fake_merge,
    ):
        call_command("merge_duplicate_users", "--yes", stdout=out, stderr=err)

    assert "failed" in err.getvalue().lower()
    # Second group still merged
    assert not User.objects.filter(pk=b2.pk).exists()
