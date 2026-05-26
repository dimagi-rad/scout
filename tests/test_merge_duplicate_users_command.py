"""Tests for the merge_duplicate_users management command."""

from io import StringIO

import pytest
from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command

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
        "merge_duplicate_users", "--dry-run", "--email", "x@y.com",
        "--canonical-id", str(newer.pk), stdout=out,
    )

    output = out.getvalue()
    assert f"canonical: User#{newer.pk}" in output

    # Invalid canonical-id raises
    with pytest.raises(CommandError):
        call_command(
            "merge_duplicate_users", "--dry-run", "--email", "x@y.com",
            "--canonical-id", "999999", stdout=out,
        )
