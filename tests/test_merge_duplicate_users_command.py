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
