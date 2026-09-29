"""The #379 cleanup: archive live OCS memberships that record no team."""

from __future__ import annotations

from datetime import timedelta
from io import StringIO

import pytest
from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command
from django.utils import timezone

from apps.users.management.commands.archive_teamless_ocs_memberships import Command
from apps.users.models import Tenant, TenantConnection, TenantMembership


def _run(*args) -> str:
    out = StringIO()
    call_command("archive_teamless_ocs_memberships", *args, stdout=out)
    return out.getvalue()


def _membership(user, tenant, connection, **metadata):
    return TenantMembership.all_objects.create(
        user=user, tenant=tenant, connection=connection, provider_metadata=metadata
    )


@pytest.fixture
def rows(user):
    other = get_user_model().objects.create_user(email="other@example.com", password="pass")
    ocs_oauth = TenantConnection.objects.create(
        user=user, provider="ocs", credential_type=TenantConnection.OAUTH
    )
    team_oauth = TenantConnection.objects.create(
        user=user, provider="ocs", credential_type=TenantConnection.OAUTH, scope_key="team-a"
    )
    commcare = TenantConnection.objects.create(
        user=user, provider="commcare", credential_type=TenantConnection.OAUTH
    )
    api_key = TenantConnection.objects.create(
        user=other, provider="ocs", credential_type=TenantConnection.API_KEY
    )
    ocs = [
        Tenant.objects.create(provider="ocs", external_id=f"exp-{i}", canonical_name=f"Bot {i}")
        for i in range(5)
    ]
    domain = Tenant.objects.create(provider="commcare", external_id="d1", canonical_name="D1")
    already = timezone.now() - timedelta(days=3)
    tombstone = _membership(other, ocs[4], None)
    tombstone.archived_at = already
    tombstone.save(update_fields=["archived_at"])
    return {
        "user": user,
        "other": other,
        "teamless": _membership(user, ocs[0], ocs_oauth),
        "blank_slug": _membership(user, ocs[1], ocs_oauth, team_slug="  ", team_name="x"),
        "no_connection": _membership(user, ocs[2], None),
        "teamed": _membership(user, ocs[3], team_oauth, team_slug="team-a"),
        "commcare": _membership(user, domain, commcare),
        "api_key": _membership(other, ocs[0], api_key),
        "tombstone": tombstone,
        "already": already,
    }


def _archived(membership):
    return TenantMembership.all_objects.get(pk=membership.pk).archived_at


@pytest.mark.django_db
def test_dry_run_reports_counts_and_writes_nothing(rows):
    out = _run()

    assert "DRY RUN" in out
    assert f"user {rows['user'].pk}: 3" in out
    assert f"user {rows['other'].pk}" not in out
    assert "(ocs exp-0): 1" in out
    assert "(ocs exp-3)" not in out
    assert "Kept 1 team-less membership(s) on an API-key connection." in out
    assert "would archive 3" in out
    assert "@" not in out
    assert TenantMembership.objects.count() == 6


@pytest.mark.django_db
def test_apply_archives_only_teamless_ocs_rows(rows, mocker, django_capture_on_commit_callbacks):
    invalidate = mocker.patch(
        "apps.users.management.commands.archive_teamless_ocs_memberships.access_cache.invalidate"
    )

    with django_capture_on_commit_callbacks(execute=True):
        out = _run("--apply")

    assert "Archived 3 membership(s) for 1 user(s)." in out
    for key in ("teamless", "blank_slug", "no_connection"):
        assert _archived(rows[key]) is not None, key
    for key in ("teamed", "commcare", "api_key"):
        assert _archived(rows[key]) is None, key
    assert _archived(rows["tombstone"]) == rows["already"]
    invalidate.assert_called_once_with(user_id=rows["user"].pk)


@pytest.mark.django_db
def test_second_apply_is_a_noop(rows):
    _run("--apply")
    first = {key: _archived(rows[key]) for key in ("teamless", "blank_slug", "no_connection")}

    out = _run("--apply")

    assert "Archived 0 membership(s) for 0 user(s)." in out
    assert {key: _archived(rows[key]) for key in first} == first
    assert TenantMembership.objects.count() == 3


@pytest.mark.django_db
def test_apply_refuses_under_any_of_access(rows, settings):
    settings.WORKSPACE_ACCESS_REQUIRES_EVERY_TENANT = False

    with pytest.raises(CommandError, match="WORKSPACE_ACCESS_REQUIRES_EVERY_TENANT"):
        _run("--apply")

    assert _archived(rows["teamless"]) is None
    assert "would archive 3" in _run()


@pytest.mark.django_db
def test_user_id_scopes_the_run(rows):
    out = _run("--apply", "--user-id", str(rows["other"].pk))

    assert "Archived 0 membership(s) for 0 user(s)." in out
    assert _archived(rows["teamless"]) is None


@pytest.mark.django_db
def test_apply_writes_only_what_the_scan_reported(rows, mocker):
    """A row that gained a team after the scan is kept, and one created after it is
    not archived unreported."""
    archive = Command._archive_for_user
    late = {}

    def race_then_archive(self, user_id, scanned):
        rows["teamless"].team_slug = "team-a"
        rows["teamless"].save(update_fields=["provider_metadata"])
        tenant = Tenant.objects.create(provider="ocs", external_id="late", canonical_name="Late")
        late["row"] = _membership(rows["user"], tenant, None)
        return archive(self, user_id, scanned)

    mocker.patch.object(Command, "_archive_for_user", race_then_archive)

    out = _run("--apply")

    assert "Archived 2 membership(s) for 1 user(s)." in out
    assert _archived(rows["teamless"]) is None
    assert _archived(late["row"]) is None
    assert _archived(rows["blank_slug"]) is not None


@pytest.mark.django_db
def test_dry_run_names_users_left_without_a_data_source(rows):
    stranded = get_user_model().objects.create_user(email="stranded@example.com", password="p")
    conn = TenantConnection.objects.create(
        user=stranded, provider="ocs", credential_type=TenantConnection.OAUTH
    )
    tenant = Tenant.objects.create(provider="ocs", external_id="solo", canonical_name="Solo")
    _membership(stranded, tenant, conn)

    out = _run()

    left = out.split("Users left with no live data source")[1].splitlines()
    assert f"  user {stranded.pk}" in left
    assert f"  user {rows['user'].pk}" not in left
