"""GET /api/workspaces/ annotates each workspace with live `has_access`.

The list returns every membership (so orphaned workspaces stay addressable by
URL), but flags the ones the user has lost upstream tenant access to — the same
rule apps/workspaces/access.py gates each request with.
"""

from datetime import timedelta

import pytest
from allauth.socialaccount.models import SocialToken
from django.test import Client
from django.utils import timezone

from apps.users.models import Tenant, TenantMembership
from apps.workspaces.models import (
    Workspace,
    WorkspaceMembership,
    WorkspaceRole,
    WorkspaceTenant,
)
from apps.workspaces.services.credential_coverage import CoverageRecovery, member_coverage_gaps
from tests.tenant_access import grant_ocs_team_access, ocs_team_connection


@pytest.fixture
def client():
    return Client(enforce_csrf_checks=False)


def _entry(resp, workspace_id):
    return next(w for w in resp.json() if w["id"] == str(workspace_id))


@pytest.mark.django_db
def test_member_with_live_tenant_has_access(client, user, workspace):
    client.force_login(user)
    resp = client.get("/api/workspaces/")
    assert resp.status_code == 200
    assert _entry(resp, workspace.id)["has_access"] is True


@pytest.mark.django_db
def test_member_without_live_tenant_lost_access(client, user):
    tenant = Tenant.objects.create(
        provider="commcare", external_id="skelly", canonical_name="skelly"
    )
    ws = Workspace.objects.create(name="Skelly WS", created_by=user)
    WorkspaceTenant.objects.create(workspace=ws, tenant=tenant)
    WorkspaceMembership.objects.create(workspace=ws, user=user, role=WorkspaceRole.MANAGE)
    # Member, but no live TenantMembership — upstream access was removed.

    client.force_login(user)
    resp = client.get("/api/workspaces/")

    entry = _entry(resp, ws.id)
    assert entry["has_access"] is False
    assert entry["tenants"][0]["provider"] == "commcare"


@pytest.mark.django_db
def test_zero_tenant_workspace_has_no_access(client, user):
    """The gate denies a workspace with no sources (#381); the list agrees."""
    ws = Workspace.objects.create(name="Tenantless", created_by=user)
    WorkspaceMembership.objects.create(workspace=ws, user=user, role=WorkspaceRole.MANAGE)

    client.force_login(user)
    resp = client.get("/api/workspaces/")

    assert _entry(resp, ws.id)["has_access"] is False


@pytest.mark.django_db
def test_archived_tenant_membership_lost_access(client, user):
    """An archived (revoked) TenantMembership must not count as live access."""
    tenant = Tenant.objects.create(
        provider="commcare", external_id="revoked", canonical_name="Revoked Domain"
    )
    ws = Workspace.objects.create(name="Revoked WS", created_by=user)
    WorkspaceTenant.objects.create(workspace=ws, tenant=tenant)
    WorkspaceMembership.objects.create(workspace=ws, user=user, role=WorkspaceRole.MANAGE)
    TenantMembership.all_objects.create(user=user, tenant=tenant, archived_at=timezone.now())

    client.force_login(user)
    resp = client.get("/api/workspaces/")

    assert _entry(resp, ws.id)["has_access"] is False


def _ocs_workspace(user, external_id, connection, *, team_slug=None):
    tenant = Tenant.objects.create(
        provider="ocs", external_id=external_id, canonical_name=external_id
    )
    grant_ocs_team_access(user, tenant, connection, team_slug=team_slug)
    ws = Workspace.objects.create(name=external_id, created_by=user)
    WorkspaceTenant.objects.create(workspace=ws, tenant=tenant)
    WorkspaceMembership.objects.create(workspace=ws, user=user, role=WorkspaceRole.MANAGE)
    return ws


@pytest.mark.django_db
def test_expired_sign_in_on_a_present_connection_needs_reconnect(client, user):
    connection = ocs_team_connection(user, "acme")
    ws = _ocs_workspace(user, "bot-expired", connection)
    SocialToken.objects.filter(account=connection.social_account).update(
        expires_at=timezone.now() - timedelta(hours=1), token_secret=""
    )

    client.force_login(user)
    entry = _entry(client.get("/api/workspaces/"), ws.id)

    assert entry["has_access"] is False
    assert [t["recovery"] for t in entry["missing_tenants"]] == [CoverageRecovery.RECONNECT]
    assert entry["needs_reconnect"] is True


@pytest.mark.django_db
@pytest.mark.parametrize("team_slug", [None, ""], ids=["team", "pre-team"])
def test_deleted_connection_no_longer_needs_reconnect(client, user, team_slug):
    """A team the member removed from Connected Accounts stays lost, but its
    workspaces are not offered as something a reconnect would restore."""
    dropped = ocs_team_connection(user, "dimagi-dev")
    ws = _ocs_workspace(user, f"bot-dropped-{team_slug}", dropped, team_slug=team_slug)
    client.force_login(user)

    assert client.delete(f"/api/auth/connections/{dropped.id}/").status_code == 200
    entry = _entry(client.get("/api/workspaces/"), ws.id)

    assert entry["has_access"] is False
    assert entry["needs_reconnect"] is False


@pytest.mark.django_db
@pytest.mark.parametrize("team_slug", [None, ""], ids=["team", "pre-team"])
def test_provider_wide_disconnect_no_longer_needs_reconnect(client, user, team_slug):
    ws = _ocs_workspace(
        user, f"bot-signed-out-{team_slug}", ocs_team_connection(user, "acme"), team_slug=team_slug
    )
    client.force_login(user)

    assert client.post("/api/auth/providers/ocs/disconnect/").status_code == 200
    entry = _entry(client.get("/api/workspaces/"), ws.id)

    assert entry["has_access"] is False
    assert entry["needs_reconnect"] is False


@pytest.mark.django_db
def test_archived_pre_team_row_on_a_kept_connection_still_needs_reconnect(client, user):
    """Control for the deleted case: the same tombstone whose connection survives is
    fixed by reconnecting it with a team."""
    ws = _ocs_workspace(user, "bot-legacy", ocs_team_connection(user, "acme"), team_slug="")
    TenantMembership.objects.filter(user=user).update(archived_at=timezone.now())

    client.force_login(user)
    entry = _entry(client.get("/api/workspaces/"), ws.id)

    assert [t["recovery"] for t in entry["missing_tenants"]] == [
        CoverageRecovery.LEGACY_TEAM_UNKNOWN
    ]
    assert entry["needs_reconnect"] is True


@pytest.mark.django_db
def test_removed_upstream_or_never_connected_does_not_need_reconnect(client, user):
    """Neither is fixed by reconnecting: one needs an upstream admin, the other a
    source the member never had."""
    removed = Tenant.objects.create(provider="commcare", external_id="gone", canonical_name="gone")
    never = Tenant.objects.create(provider="commcare", external_id="never", canonical_name="never")
    TenantMembership.all_objects.create(user=user, tenant=removed, archived_at=timezone.now())
    workspaces = []
    for tenant in (removed, never):
        ws = Workspace.objects.create(name=tenant.external_id, created_by=user)
        WorkspaceTenant.objects.create(workspace=ws, tenant=tenant)
        WorkspaceMembership.objects.create(workspace=ws, user=user, role=WorkspaceRole.MANAGE)
        workspaces.append(ws)

    client.force_login(user)
    resp = client.get("/api/workspaces/")

    assert [_entry(resp, ws.id)["needs_reconnect"] for ws in workspaces] == [False, False]


@pytest.mark.django_db
def test_accessible_workspace_does_not_need_reconnect(client, user, workspace):
    client.force_login(user)
    assert _entry(client.get("/api/workspaces/"), workspace.id)["needs_reconnect"] is False


@pytest.mark.django_db
@pytest.mark.parametrize("keep_connection", [True, False])
def test_tombstone_is_disconnected_only_once_its_connection_is_gone(user, keep_connection):
    connection = ocs_team_connection(user, "acme")
    tenant = Tenant.objects.create(provider="ocs", external_id="bot-x", canonical_name="Bot X")
    grant_ocs_team_access(user, tenant, connection)
    TenantMembership.objects.filter(user=user).update(
        archived_at=timezone.now(), **({} if keep_connection else {"connection": None})
    )

    (missing,) = member_coverage_gaps(user.pk, [tenant]).values()

    assert missing.disconnected is not keep_connection
    assert "disconnected" not in missing.as_dict()


@pytest.mark.django_db
def test_any_of_rule_needs_one_reconnectable_source(settings, client, user):
    settings.WORKSPACE_ACCESS_REQUIRES_EVERY_TENANT = False
    connection = ocs_team_connection(user, "acme")
    legacy = Tenant.objects.create(provider="ocs", external_id="bot-l", canonical_name="Bot L")
    gone = Tenant.objects.create(provider="ocs", external_id="bot-g", canonical_name="Bot G")
    grant_ocs_team_access(user, legacy, connection, team_slug="")
    grant_ocs_team_access(user, gone, connection)
    TenantMembership.objects.filter(user=user).update(archived_at=timezone.now())
    ws = Workspace.objects.create(name="Mixed", created_by=user)
    for tenant in (legacy, gone):
        WorkspaceTenant.objects.create(workspace=ws, tenant=tenant)
    WorkspaceMembership.objects.create(workspace=ws, user=user, role=WorkspaceRole.MANAGE)

    client.force_login(user)
    entry = _entry(client.get("/api/workspaces/"), ws.id)

    assert entry["has_access"] is False
    assert entry["needs_reconnect"] is True


@pytest.mark.django_db
def test_pre_team_tombstone_that_never_had_a_connection_still_needs_reconnect(client, user):
    """A team-less row archived with no connection (#379) is restored by reconnecting
    OCS with its team, so it is not mistaken for one the member deleted."""
    ws = _ocs_workspace(user, "bot-never", ocs_team_connection(user, "acme"), team_slug="")
    TenantMembership.objects.filter(user=user).update(connection=None, archived_at=timezone.now())

    client.force_login(user)
    entry = _entry(client.get("/api/workspaces/"), ws.id)

    assert [t["recovery"] for t in entry["missing_tenants"]] == [
        CoverageRecovery.LEGACY_TEAM_UNKNOWN
    ]
    assert entry["needs_reconnect"] is True
