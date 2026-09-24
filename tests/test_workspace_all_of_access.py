"""All-of workspace access (#380): a member must cover EVERY workspace tenant.

A multi-source workspace serves one merged view, so a member who can use only
some of its sources would read the rest. These tests drive the real authorizer
with genuinely provisioned credentials and pin: partial coverage is denied on
every read path with actionable per-source guidance, full coverage is granted,
zero-tenant workspaces are unchanged, and the list agrees with the gate.
"""

import json
import uuid

import pytest
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from django.test import AsyncClient
from django.utils import timezone

from apps.users.models import Tenant, TenantMembership
from apps.workspaces.access import (
    TENANT_ACCESS_LOST,
    access_denied_body,
    aresolve_workspace_access_ex,
    resolve_workspace_access_ex,
)
from apps.workspaces.models import Workspace, WorkspaceMembership, WorkspaceRole, WorkspaceTenant
from apps.workspaces.services.credential_coverage import CoverageRecovery
from mcp_server.server import semantic_catalog
from tests.tenant_access import (
    grant_ocs_team_access,
    grant_tenant_access,
    ocs_team_connection,
)

User = get_user_model()


def _tenant(external_id, name, provider="commcare"):
    return Tenant.objects.create(provider=provider, external_id=external_id, canonical_name=name)


def _workspace(owner, *tenants, name="Two sources"):
    ws = Workspace.objects.create(name=name, created_by=owner)
    for tenant in tenants:
        WorkspaceTenant.objects.create(workspace=ws, tenant=tenant)
    return ws


def _join(ws, user, role=WorkspaceRole.READ):
    WorkspaceMembership.objects.create(workspace=ws, user=user, role=role)


@pytest.fixture
def two_sources(db):
    return _tenant("t1", "Source One"), _tenant("t2", "Source Two")


@pytest.fixture
def partial_member(db, user, two_sources):
    """``user`` covers only the first of a two-source workspace."""
    t1, t2 = two_sources
    ws = _workspace(user, t1, t2)
    _join(ws, user)
    grant_tenant_access(user, t1)
    return ws


def _missing(result):
    return [(t.tenant_name, t.recovery) for t in result.missing_tenants]


@pytest.mark.django_db
def test_partial_coverage_is_denied_and_names_only_the_missing_source(user, partial_member):
    result = resolve_workspace_access_ex(user, partial_member.id)

    assert not result.granted
    assert result.denied_reason == TENANT_ACCESS_LOST
    assert _missing(result) == [("Source Two", CoverageRecovery.CONNECT_SOURCE)]
    body = access_denied_body(result)
    assert body["reason"] == TENANT_ACCESS_LOST
    assert body["lost_tenants"] == ["Source Two"]
    assert [t["tenant_name"] for t in body["missing_tenants"]] == ["Source Two"]
    assert "Source Two" in body["error"]
    assert "Source One" not in body["error"]
    assert "Connected Accounts" in body["error"]


@pytest.mark.django_db
def test_full_coverage_is_granted(user, partial_member, two_sources):
    grant_tenant_access(user, two_sources[1])

    assert resolve_workspace_access_ex(user, partial_member.id).granted


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_async_gate_matches_sync_gate():
    user = await User.objects.acreate_user(email="async-allof@example.com", password="pass")
    _t1, t2, ws = await sync_to_async(_async_fixture)(user)

    denied = await aresolve_workspace_access_ex(user, ws.id)
    assert denied.denied_reason == TENANT_ACCESS_LOST
    assert [t.tenant_id for t in denied.missing_tenants] == [str(t2.id)]

    await sync_to_async(grant_tenant_access)(user, t2)
    assert (await aresolve_workspace_access_ex(user, ws.id)).granted


def _async_fixture(user):
    t1, t2 = _tenant("a1", "A1"), _tenant("a2", "A2")
    ws = _workspace(user, t1, t2)
    _join(ws, user)
    grant_tenant_access(user, t1)
    return t1, t2, ws


@pytest.mark.django_db
def test_zero_tenant_workspace_needs_only_membership(user):
    ws = _workspace(user)
    _join(ws, user)

    assert resolve_workspace_access_ex(user, ws.id).granted


@pytest.mark.django_db
def test_live_membership_without_a_usable_credential_does_not_cover(user, two_sources):
    """Row presence is not coverage: a membership whose credential cannot be
    resolved would pass a row count while every read of that tenant fails."""
    t1, t2 = two_sources
    ws = _workspace(user, t1, t2)
    _join(ws, user)
    grant_tenant_access(user, t1)
    TenantMembership.objects.create(user=user, tenant=t2)

    result = resolve_workspace_access_ex(user, ws.id)

    assert _missing(result) == [("Source Two", CoverageRecovery.RECONNECT)]


@pytest.mark.django_db
def test_removed_access_is_distinguished_from_never_having_it(user, two_sources):
    t1, t2 = two_sources
    t3 = _tenant("t3", "Source Three")
    ws = _workspace(user, t1, t2, t3)
    _join(ws, user)
    grant_tenant_access(user, t1)
    grant_tenant_access(user, t2)
    TenantMembership.objects.filter(user=user, tenant=t2).update(archived_at=timezone.now())

    result = resolve_workspace_access_ex(user, ws.id)

    assert sorted(_missing(result)) == [
        ("Source Three", CoverageRecovery.CONNECT_SOURCE),
        ("Source Two", CoverageRecovery.ACCESS_REMOVED),
    ]


@pytest.mark.django_db
def test_ocs_team_gaps_name_the_team_to_connect(user):
    """Wrong-team and unknown-legacy-team OCS rows get distinct, team-specific
    guidance — they are fixed by connecting a team, not by asking for access."""
    bot_a = _tenant("bot-a", "Bot A", provider="ocs")
    bot_b = _tenant("bot-b", "Bot B", provider="ocs")
    bot_legacy = _tenant("bot-legacy", "Bot Legacy", provider="ocs")
    team_a = ocs_team_connection(user, "team-a")
    grant_ocs_team_access(user, bot_a, team_a)
    grant_ocs_team_access(user, bot_b, team_a, team_slug="team-b")
    grant_ocs_team_access(user, bot_legacy, team_a, team_slug="")
    ws = _workspace(user, bot_a, bot_b, bot_legacy)
    _join(ws, user)

    result = resolve_workspace_access_ex(user, ws.id)

    assert sorted(_missing(result)) == [
        ("Bot B", CoverageRecovery.CONNECT_TEAM),
        ("Bot Legacy", CoverageRecovery.LEGACY_TEAM_UNKNOWN),
    ]
    error = access_denied_body(result)["error"]
    assert "connect Open Chat Studio team 'Team-B'" in error
    assert "choosing the team that owns it" in error


@pytest.mark.django_db
def test_two_team_member_covers_a_workspace_spanning_both_teams(user):
    bot_a = _tenant("bot-a", "Bot A", provider="ocs")
    bot_b = _tenant("bot-b", "Bot B", provider="ocs")
    grant_ocs_team_access(user, bot_a, ocs_team_connection(user, "team-a"))
    grant_ocs_team_access(user, bot_b, ocs_team_connection(user, "team-b"))
    ws = _workspace(user, bot_a, bot_b)
    _join(ws, user)

    assert resolve_workspace_access_ex(user, ws.id).granted


@pytest.mark.django_db
def test_outsider_gets_generic_denial_without_source_details(other_user, partial_member):
    result = resolve_workspace_access_ex(other_user, partial_member.id)

    assert access_denied_body(result) == {"error": "Workspace not found or access denied."}


@pytest.mark.django_db
def test_rollout_switch_off_keeps_the_any_of_rule(settings, user, partial_member):
    settings.WORKSPACE_ACCESS_REQUIRES_EVERY_TENANT = False

    assert resolve_workspace_access_ex(user, partial_member.id).granted
    TenantMembership.objects.filter(user=user).update(archived_at=timezone.now())
    denied = resolve_workspace_access_ex(user, partial_member.id)
    assert sorted(_missing(denied)) == [
        ("Source One", CoverageRecovery.ACCESS_REMOVED),
        ("Source Two", CoverageRecovery.CONNECT_SOURCE),
    ]


def _list_entry(client, ws):
    resp = client.get("/api/workspaces/")
    assert resp.status_code == 200
    return next(e for e in resp.json() if e["id"] == str(ws.id))


@pytest.mark.django_db
def test_list_has_access_agrees_with_the_gate(client, user, partial_member, two_sources):
    client.force_login(user)
    zero = _workspace(user, name="Empty")
    _join(zero, user)

    entry = _list_entry(client, partial_member)
    assert entry["has_access"] is False
    assert [(t["tenant_name"], t["recovery"]) for t in entry["missing_tenants"]] == [
        ("Source Two", CoverageRecovery.CONNECT_SOURCE)
    ]
    assert entry["missing_tenants"][0]["remedy"]
    assert _list_entry(client, zero)["has_access"] is True
    assert _list_entry(client, zero)["missing_tenants"] == []

    grant_tenant_access(user, two_sources[1])
    entry = _list_entry(client, partial_member)
    assert entry["has_access"] is True
    assert entry["missing_tenants"] == []
    assert resolve_workspace_access_ex(user, partial_member.id).granted


@pytest.mark.django_db
@pytest.mark.parametrize(
    "path",
    [
        "/api/workspaces/{ws}/knowledge/",
        "/api/workspaces/{ws}/knowledge/export/",
        "/api/workspaces/{ws}/artifacts/{artifact}/data/",
        "/api/workspaces/{ws}/artifacts/{artifact}/export/html/",
    ],
)
def test_partial_member_is_denied_on_http_reads(client, user, partial_member, path):
    client.force_login(user)

    resp = client.get(path.format(ws=partial_member.id, artifact=uuid.uuid4()))

    assert resp.status_code == 403
    body = resp.json()
    assert body["reason"] == TENANT_ACCESS_LOST
    assert [t["tenant_name"] for t in body["missing_tenants"]] == ["Source Two"]


def _async_partial_member(email):
    user = User.objects.create_user(email=email, password="pass")
    t1, t2 = _tenant(f"{email}-1", "Source One"), _tenant(f"{email}-2", "Source Two")
    ws = _workspace(user, t1, t2)
    _join(ws, user, role=WorkspaceRole.MANAGE)
    grant_tenant_access(user, t1)
    return user, ws


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "path",
    ["/api/workspaces/{ws}/threads/", "/api/workspaces/{ws}/artifacts/{artifact}/query-data/"],
)
async def test_partial_member_is_denied_on_async_reads(path):
    user, ws = await sync_to_async(_async_partial_member)("async-reads@example.com")
    client = AsyncClient()
    await client.aforce_login(user)

    resp = await client.get(path.format(ws=ws.id, artifact=uuid.uuid4()))

    assert resp.status_code == 403
    assert resp.json()["reason"] == TENANT_ACCESS_LOST


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_partial_member_cannot_start_a_chat_turn():
    user, ws = await sync_to_async(_async_partial_member)("chat-partial@example.com")
    client = AsyncClient()
    await client.aforce_login(user)

    resp = await client.post(
        "/api/chat/",
        data=json.dumps(
            {"messages": [{"role": "user", "content": "count rows"}], "workspaceId": str(ws.id)}
        ),
        content_type="application/json",
    )

    assert resp.status_code == 403
    body = resp.json()
    assert body["reason"] == TENANT_ACCESS_LOST
    assert [t["tenant_name"] for t in body["missing_tenants"]] == ["Source Two"]


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_partial_member_is_denied_by_user_scoped_mcp_tools():
    user, ws = await sync_to_async(_async_partial_member)("mcp-partial@example.com")

    result = await semantic_catalog(workspace_id=str(ws.id), user_id=str(user.id))

    assert result["success"] is False
    assert result["error"]["code"] == "NOT_FOUND"


@pytest.mark.django_db
def test_list_readiness_is_bulk_not_per_workspace(
    client, user, two_sources, django_assert_max_num_queries
):
    """``has_access`` for many workspaces costs the same query count as for one."""
    client.force_login(user)
    t1, t2 = two_sources
    grant_tenant_access(user, t1)
    ws = _workspace(user, t1, t2)
    _join(ws, user)
    client.get("/api/workspaces/")
    with django_assert_max_num_queries(20) as one:
        client.get("/api/workspaces/")
    for n in range(5):
        extra = _workspace(user, _tenant(f"x{n}", f"Extra {n}"), t2, name=f"W{n}")
        _join(extra, user)

    with django_assert_max_num_queries(len(one.captured_queries)):
        resp = client.get("/api/workspaces/")

    assert all(entry["has_access"] is False for entry in resp.json())


@pytest.mark.django_db
def test_denial_wording_follows_the_rule_in_force(settings, user, two_sources):
    t1, t2 = two_sources
    ws = _workspace(user, t1, t2)
    _join(ws, user)

    assert (
        "every one of its data sources"
        in access_denied_body(resolve_workspace_access_ex(user, ws.id))["error"]
    )
    settings.WORKSPACE_ACCESS_REQUIRES_EVERY_TENANT = False
    assert "at least one" in access_denied_body(resolve_workspace_access_ex(user, ws.id))["error"]


@pytest.mark.django_db
class TestRemediationWithoutCoverage:
    """A member who lost a source for good must still be able to fix it in Scout:
    remove the source, leave, or delete the workspace — none of which reads data."""

    @pytest.fixture
    def manager(self, user, partial_member):
        WorkspaceMembership.objects.filter(workspace=partial_member, user=user).update(
            role=WorkspaceRole.MANAGE
        )
        return user

    def test_can_list_and_remove_the_missing_source_and_regain_access(
        self, client, manager, partial_member, two_sources
    ):
        client.force_login(manager)
        sources = client.get(f"/api/workspaces/{partial_member.id}/tenants/").json()
        missing = next(s for s in sources if s["tenant_id"] == str(two_sources[1].id))

        resp = client.delete(f"/api/workspaces/{partial_member.id}/tenants/{missing['id']}/")

        assert resp.status_code == 204
        assert resolve_workspace_access_ex(manager, partial_member.id).granted

    def test_can_leave(self, client, manager, partial_member, other_user):
        _join(partial_member, other_user, role=WorkspaceRole.MANAGE)
        mine = WorkspaceMembership.objects.get(workspace=partial_member, user=manager)
        client.force_login(manager)

        resp = client.delete(f"/api/workspaces/{partial_member.id}/members/{mine.id}/")

        assert resp.status_code == 204

    def test_cannot_remove_other_members(self, client, manager, partial_member, other_user):
        _join(partial_member, other_user)
        theirs = WorkspaceMembership.objects.get(workspace=partial_member, user=other_user)
        client.force_login(manager)

        resp = client.delete(f"/api/workspaces/{partial_member.id}/members/{theirs.id}/")

        assert resp.status_code == 403
        assert WorkspaceMembership.objects.filter(pk=theirs.pk).exists()

    def test_can_delete_the_workspace(self, client, manager, partial_member, two_sources):
        # Deletion separately refuses to drop a user's last workspace for a source.
        spare = _workspace(manager, *two_sources, name="Spare")
        _join(spare, manager, role=WorkspaceRole.MANAGE)
        client.force_login(manager)

        resp = client.delete(f"/api/workspaces/{partial_member.id}/")

        assert resp.status_code == 204

    def test_can_reach_the_page_that_offers_the_fixes(
        self, client, manager, partial_member, other_user
    ):
        _join(partial_member, other_user)
        Workspace.objects.filter(pk=partial_member.pk).update(system_prompt="secret instructions")
        client.force_login(manager)

        detail = client.get(f"/api/workspaces/{partial_member.id}/")
        assert detail.status_code == 200
        assert detail.json()["system_prompt"] == ""
        roster = client.get(f"/api/workspaces/{partial_member.id}/members/").json()
        # Only their own row (for leaving), not the other members or invites.
        assert [m["user_id"] for m in roster["members"]] == [str(manager.id)]
        assert roster["invites"] == []

    def test_cannot_delete_a_workspace_others_are_in(
        self, client, manager, partial_member, other_user, two_sources
    ):
        _join(partial_member, other_user)
        spare = _workspace(manager, *two_sources, name="Spare")
        _join(spare, manager, role=WorkspaceRole.MANAGE)
        client.force_login(manager)

        resp = client.delete(f"/api/workspaces/{partial_member.id}/")

        assert resp.status_code == 403
        assert Workspace.objects.filter(pk=partial_member.pk).exists()

    def test_cannot_remove_a_source_they_still_have(
        self, client, manager, partial_member, two_sources
    ):
        client.force_login(manager)
        sources = client.get(f"/api/workspaces/{partial_member.id}/tenants/").json()
        kept = next(s for s in sources if s["tenant_id"] == str(two_sources[0].id))

        resp = client.delete(f"/api/workspaces/{partial_member.id}/tenants/{kept['id']}/")

        assert resp.status_code == 403
        assert WorkspaceTenant.objects.filter(pk=kept["id"]).exists()

    def test_still_cannot_read_workspace_content(self, client, manager, partial_member):
        client.force_login(manager)

        assert client.get(f"/api/workspaces/{partial_member.id}/knowledge/").status_code == 403
        assert client.get(f"/api/workspaces/{partial_member.id}/artifacts/").status_code == 403
        assert (
            client.get(f"/api/workspaces/{partial_member.id}/knowledge/export/").status_code == 403
        )


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/api/workspaces/{ws}/"),
        ("get", "/api/workspaces/{ws}/members/"),
        ("get", "/api/workspaces/{ws}/tenants/"),
        ("delete", "/api/workspaces/{ws}/"),
    ],
)
def test_switch_off_keeps_the_any_of_decision_on_exempt_paths(
    settings, client, user, two_sources, method, path
):
    """With the switch off nothing is exempt: a member with no live source is
    denied remediation paths exactly as before #380."""
    settings.WORKSPACE_ACCESS_REQUIRES_EVERY_TENANT = False
    ws = _workspace(user, *two_sources)
    _join(ws, user, role=WorkspaceRole.MANAGE)
    client.force_login(user)

    resp = getattr(client, method)(path.format(ws=ws.id))

    assert resp.status_code == 403
    assert Workspace.objects.filter(pk=ws.pk).exists()


@pytest.mark.django_db
def test_switch_off_list_matches_the_any_of_rule(settings, client, user, partial_member):
    settings.WORKSPACE_ACCESS_REQUIRES_EVERY_TENANT = False
    client.force_login(user)

    entry = _list_entry(client, partial_member)
    assert entry["has_access"] is True
    assert entry["missing_tenants"] == []

    TenantMembership.objects.filter(user=user).update(archived_at=timezone.now())
    assert _list_entry(client, partial_member)["has_access"] is False
