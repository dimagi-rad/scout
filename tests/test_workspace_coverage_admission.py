"""Membership and source changes keep every member covering every tenant (#381).

The read gate denies a partially covering member; these tests pin that such a
member is never created in the first place: adding a source another member
cannot use is refused atomically with who/what guidance, direct add and invite
acceptance admit only full coverage (otherwise the invite awaits access), and
workspace creation validates every requested source.
"""

from datetime import timedelta
from unittest.mock import patch

import pytest
from asgiref.sync import async_to_sync, sync_to_async
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient

from apps.users.models import Tenant, TenantMembership
from apps.users.signals import resolve_pending_invites_on_login
from apps.workspaces.api import workspace_views
from apps.workspaces.models import (
    Workspace,
    WorkspaceInvite,
    WorkspaceInviteStatus,
    WorkspaceMembership,
    WorkspaceRole,
    WorkspaceTenant,
)
from apps.workspaces.services import invite_notifications, member_coverage
from apps.workspaces.services.credential_coverage import CoverageRecovery
from apps.workspaces.services.invite_notifications import describe_workspace_sources
from apps.workspaces.services.member_coverage import (
    MembersLackTenant,
    accept_invite_if_covered,
    add_tenant_covered_by_members,
    admit_covered_member,
)
from tests.row_locks import row_locked
from tests.tenant_access import grant_tenant_access, ocs_team_connection

User = get_user_model()

REFRESH = "apps.workspaces.api.workspace_views._arefresh_target_for_workspace"


def _tenant(external_id, name, provider="commcare"):
    return Tenant.objects.create(provider=provider, external_id=external_id, canonical_name=name)


def _workspace(owner, *tenants):
    ws = Workspace.objects.create(name="Shared", created_by=owner)
    for tenant in tenants:
        WorkspaceTenant.objects.create(workspace=ws, tenant=tenant)
    WorkspaceMembership.objects.create(workspace=ws, user=owner, role=WorkspaceRole.MANAGE)
    for tenant in tenants:
        grant_tenant_access(owner, tenant)
    return ws


def _member(ws, email, *tenants, role=WorkspaceRole.READ):
    user = User.objects.create_user(email=email, password="pass", first_name=email.split("@")[0])
    WorkspaceMembership.objects.create(workspace=ws, user=user, role=role)
    for tenant in tenants:
        grant_tenant_access(user, tenant)
    return user


async def _no_refresh(*_args, **_kwargs):
    return False


@pytest.fixture
def client():
    return APIClient()


@pytest.fixture
def t1(db):
    return _tenant("t1", "Source One")


@pytest.fixture
def t2(db):
    return _tenant("t2", "Source Two")


@pytest.mark.django_db
class TestSourceAdd:
    def _post(self, client, ws, tenant):
        return client.post(
            f"/api/workspaces/{ws.id}/tenants/", {"tenant_id": str(tenant.id)}, format="json"
        )

    @pytest.mark.parametrize(("strict", "status"), [(False, 202), (True, 409)])
    def test_switch_off_source_add_follows_the_strictness_setting(
        self, settings, monkeypatch, client, user, t1, t2, strict, status
    ):
        settings.WORKSPACE_ACCESS_REQUIRES_EVERY_TENANT = False
        monkeypatch.setattr(member_coverage, "ADMISSION_ALWAYS_ALL_OF", strict)
        ws = _workspace(user, t1)
        _member(ws, "brian@example.com", t1)
        grant_tenant_access(user, t2)
        client.force_login(user)

        with patch(REFRESH, side_effect=_no_refresh):
            resp = self._post(client, ws, t2)

        assert resp.status_code == status

    def test_readding_an_attached_source_does_no_member_refresh(self, client, user, t1):
        ws = _workspace(user, t1)
        _member(ws, "brian@example.com")
        client.force_login(user)

        with patch(REFRESH, side_effect=_no_refresh) as refresh:
            resp = self._post(client, ws, t1)

        assert resp.status_code == 200
        refresh.assert_not_called()

    def test_one_members_refresh_failure_does_not_fail_the_request(self, client, user, t1, t2):
        ws = _workspace(user, t1)
        _member(ws, "brian@example.com", t1)
        grant_tenant_access(user, t2)
        client.force_login(user)

        _member(ws, "carol@example.com", t1)

        with patch(REFRESH, side_effect=RuntimeError("provider down")) as refresh:
            resp = self._post(client, ws, t2)

        assert resp.status_code == 409
        assert resp.json()["reason"] == "members_lack_source"
        assert refresh.call_count == 2  # one failure does not stop the others

    def test_refused_when_another_member_cannot_use_it(self, client, user, t1, t2):
        ws = _workspace(user, t1)
        _member(ws, "brian@example.com", t1)
        grant_tenant_access(user, t2)
        client.force_login(user)

        with patch(REFRESH, side_effect=_no_refresh) as refresh:
            resp = self._post(client, ws, t2)

        assert resp.status_code == 409
        body = resp.json()
        assert body["reason"] == "members_lack_source"
        assert "brian@example.com" in body["error"]
        assert "separate workspace" in body["error"]
        assert [(m["email"], m["recovery"]) for m in body["members"]] == [
            ("brian@example.com", CoverageRecovery.CONNECT_SOURCE)
        ]
        assert not WorkspaceTenant.objects.filter(workspace=ws, tenant=t2).exists()
        refresh.assert_called_once()
        assert refresh.call_args.args[1] == ["commcare"]
        assert refresh.call_args.kwargs == {"renew_tokens": False}

    def test_member_refreshed_into_coverage_lets_the_add_through(self, client, user, t1, t2):
        ws = _workspace(user, t1)
        brian = _member(ws, "brian@example.com", t1)
        grant_tenant_access(user, t2)
        client.force_login(user)

        async def _grant(target, _providers, **_kwargs):
            await sync_to_async(grant_tenant_access)(target, t2)
            return True

        with patch(REFRESH, side_effect=_grant):
            resp = self._post(client, ws, t2)

        assert resp.status_code == 202
        assert WorkspaceTenant.objects.filter(workspace=ws, tenant=t2).exists()
        assert TenantMembership.objects.filter(user=brian, tenant=t2).exists()

    def test_allowed_when_every_member_covers_it(self, client, user, t1, t2):
        ws = _workspace(user, t1)
        _member(ws, "brian@example.com", t1, t2)
        grant_tenant_access(user, t2)
        client.force_login(user)

        with patch(REFRESH, side_effect=_no_refresh) as refresh:
            resp = self._post(client, ws, t2)

        assert resp.status_code == 202
        refresh.assert_not_called()

    def test_first_source_of_a_zero_tenant_workspace_is_checked_too(self, client, user, t1):
        ws = _workspace(user)
        _member(ws, "brian@example.com")
        grant_tenant_access(user, t1)
        client.force_login(user)

        with patch(REFRESH, side_effect=_no_refresh):
            resp = self._post(client, ws, t1)

        assert resp.status_code == 409
        assert not ws.workspace_tenants.exists()

    def test_requester_without_a_usable_credential_is_refused(self, client, user, t1, t2):
        ws = _workspace(user, t1)
        TenantMembership.objects.create(user=user, tenant=t2)
        client.force_login(user)

        resp = self._post(client, ws, t2)

        assert resp.status_code == 400
        assert resp.json()["error"].startswith("You can't add this source until")
        assert [t["recovery"] for t in resp.json()["missing_tenants"]] == ["reconnect"]

    def test_service_refuses_without_the_views_precheck(self, user, t1, t2):
        """The view's pre-check only decides who to refresh; the locked check decides."""
        ws = _workspace(user, t1)
        grant_tenant_access(user, t2)
        _member(ws, "late@example.com", t1)

        with pytest.raises(MembersLackTenant) as exc:
            add_tenant_covered_by_members(ws, t2)

        assert [u.email for u, _gap in exc.value.gaps] == ["late@example.com"]


@pytest.mark.django_db
class TestDirectAdd:
    def _add(self, client, ws, email):
        return client.post(
            f"/api/workspaces/{ws.id}/members/", {"email": email, "role": "read"}, format="json"
        )

    def test_partial_coverage_target_gets_an_awaiting_invite(self, client, user, t1, t2):
        ws = _workspace(user, t1, t2)
        target = User.objects.create_user(email="partial@example.com", password="pass")
        grant_tenant_access(target, t1)
        client.force_login(user)

        with patch(REFRESH, side_effect=_no_refresh) as refresh:
            resp = self._add(client, ws, target.email)

        assert resp.status_code == 201
        assert resp.json()["result"] == "invite_awaiting_access"
        assert not WorkspaceMembership.objects.filter(workspace=ws, user=target).exists()
        assert refresh.call_args.args[1] == ["commcare"]

    @pytest.mark.parametrize(
        ("strict", "expected"), [(False, "member"), (True, "invite_awaiting_access")]
    )
    def test_switch_off_admission_follows_the_strictness_setting(
        self, settings, monkeypatch, client, user, t1, t2, strict, expected
    ):
        settings.WORKSPACE_ACCESS_REQUIRES_EVERY_TENANT = False
        monkeypatch.setattr(member_coverage, "ADMISSION_ALWAYS_ALL_OF", strict)
        ws = _workspace(user, t1, t2)
        target = User.objects.create_user(email="partial@example.com", password="pass")
        grant_tenant_access(target, t1)
        client.force_login(user)

        with patch(REFRESH, side_effect=_no_refresh):
            resp = self._add(client, ws, target.email)

        assert resp.json()["result"] == expected

    def test_full_coverage_target_becomes_a_member(self, client, user, t1, t2):
        ws = _workspace(user, t1, t2)
        target = User.objects.create_user(email="full@example.com", password="pass")
        grant_tenant_access(target, t1)
        grant_tenant_access(target, t2)
        client.force_login(user)

        resp = self._add(client, ws, target.email)

        assert resp.status_code == 201
        assert resp.json()["result"] == "member"

    def test_existing_member_who_lost_a_source_gets_409_not_an_invite(self, client, user, t1, t2):
        ws = _workspace(user, t1, t2)
        member = _member(ws, "member@example.com", t1)
        client.force_login(user)

        with patch(REFRESH, side_effect=_no_refresh):
            resp = self._add(client, ws, member.email)

        assert resp.status_code == 409
        assert not WorkspaceInvite.objects.filter(workspace=ws, email=member.email).exists()

    def test_zero_tenant_workspace_admits_directly(self, client, user):
        ws = _workspace(user)
        target = User.objects.create_user(email="empty@example.com", password="pass")
        client.force_login(user)

        resp = self._add(client, ws, target.email)

        assert resp.status_code == 201
        assert resp.json()["result"] == "member"


@pytest.mark.django_db
class TestInviteResolution:
    def _invite(self, ws, email, role=WorkspaceRole.READ_WRITE):
        return WorkspaceInvite.objects.create(
            workspace=ws, email=email, role=role, status=WorkspaceInviteStatus.PENDING
        )

    def test_partial_coverage_invitee_awaits_access(self, user, t1, t2):
        ws = _workspace(user, t1, t2)
        invitee = User.objects.create_user(email="inv@example.com", password="pass")
        grant_tenant_access(invitee, t1)
        invite = self._invite(ws, invitee.email)

        resolve_pending_invites_on_login(invitee)

        invite.refresh_from_db()
        assert invite.status == WorkspaceInviteStatus.AWAITING_ACCESS
        assert not WorkspaceMembership.objects.filter(workspace=ws, user=invitee).exists()

    def test_full_coverage_invitee_joins(self, user, t1, t2):
        ws = _workspace(user, t1, t2)
        invitee = User.objects.create_user(email="inv@example.com", password="pass")
        grant_tenant_access(invitee, t1)
        grant_tenant_access(invitee, t2)
        invite = self._invite(ws, invitee.email)

        resolve_pending_invites_on_login(invitee)

        invite.refresh_from_db()
        assert invite.status == WorkspaceInviteStatus.ACCEPTED
        assert WorkspaceMembership.objects.get(workspace=ws, user=invitee).role == "read_write"

    def test_accepting_a_stale_invite_never_promotes(self, user, t1):
        ws = _workspace(user, t1)
        member = _member(ws, "m@example.com", t1, role=WorkspaceRole.READ)
        invite = self._invite(ws, member.email, role=WorkspaceRole.MANAGE)

        accept_invite_if_covered(invite, member)

        assert WorkspaceMembership.objects.get(workspace=ws, user=member).role == "read"


@pytest.mark.django_db
class TestCreate:
    @pytest.mark.parametrize(("strict", "status"), [(False, 201), (True, 400)])
    def test_switch_off_create_follows_the_strictness_setting(
        self, settings, monkeypatch, client, user, t1, t2, strict, status
    ):
        # A live but unusable membership: enough for any-of, not for all-of.
        settings.WORKSPACE_ACCESS_REQUIRES_EVERY_TENANT = False
        monkeypatch.setattr(member_coverage, "ADMISSION_ALWAYS_ALL_OF", strict)
        grant_tenant_access(user, t1)
        TenantMembership.objects.create(user=user, tenant=t2)
        client.force_login(user)

        resp = client.post(
            "/api/workspaces/",
            {"name": "Both", "tenant_ids": [str(t1.id), str(t2.id)]},
            format="json",
        )

        assert resp.status_code == status

    def test_every_requested_source_needs_a_usable_credential(self, client, user, t1, t2):
        grant_tenant_access(user, t1)
        TenantMembership.objects.create(user=user, tenant=t2)
        client.force_login(user)

        resp = client.post(
            "/api/workspaces/",
            {"name": "Both", "tenant_ids": [str(t1.id), str(t2.id)]},
            format="json",
        )

        assert resp.status_code == 400
        assert [t["tenant_name"] for t in resp.json()["missing_tenants"]] == ["Source Two"]
        assert not Workspace.objects.filter(name="Both").exists()

    def test_covered_sources_create_the_workspace(self, client, user, t1, t2):
        grant_tenant_access(user, t1)
        grant_tenant_access(user, t2)
        client.force_login(user)

        resp = client.post(
            "/api/workspaces/",
            {"name": "Both", "tenant_ids": [str(t1.id), str(t2.id)]},
            format="json",
        )

        assert resp.status_code == 201
        ws = Workspace.objects.get(name="Both")
        assert ws.workspace_tenants.count() == 2


@pytest.mark.django_db
def test_awaiting_invite_banner_names_what_is_still_needed(client, user, t1, t2, mocker):
    ws = _workspace(user, t1, t2)
    invitee = User.objects.create_user(email="inv@example.com", password="pass")
    grant_tenant_access(invitee, t1)
    WorkspaceInvite.objects.create(
        workspace=ws,
        email=invitee.email,
        role=WorkspaceRole.READ,
        status=WorkspaceInviteStatus.AWAITING_ACCESS,
        expires_at=timezone.now() + timedelta(days=7),
    )
    client.force_login(invitee)
    send = mocker.patch.object(invite_notifications, "send_email")

    message = client.get("/api/invites/").json()[0]["message"]

    assert "Source Two" in message
    assert "Source One" not in message
    send.defer.assert_not_called()


@pytest.mark.django_db
def test_invite_notices_ask_for_every_source(user, t1, t2):
    phrase = describe_workspace_sources(_workspace(user, t1, t2))

    assert " and " in phrase
    assert " or " not in phrase


@pytest.mark.django_db(transaction=True)
class TestMutationRaces:
    """Admission and source add serialize on the workspace row (#381):
    whichever commits second re-evaluates against the other's change."""

    def test_member_admission_waits_for_a_concurrent_source_add(self, user, t1, t2):
        ws = _workspace(user, t1)
        grant_tenant_access(user, t2)
        target = User.objects.create_user(email="racer@example.com", password="pass")
        grant_tenant_access(target, t1)

        def add_source_under_lock():
            Workspace.objects.select_for_update().get(pk=ws.pk)
            WorkspaceTenant.objects.create(workspace=ws, tenant=t2)

        with row_locked(add_source_under_lock, release_after=0.5):
            membership, _created, missing = admit_covered_member(
                ws, target, role=WorkspaceRole.READ, invited_by=user
            )

        assert membership is None
        assert [t.tenant_name for t in missing] == ["Source Two"]

    def test_source_add_waits_for_a_concurrent_member_admission(self, user, t1, t2):
        ws = _workspace(user, t1)
        grant_tenant_access(user, t2)
        target = User.objects.create_user(email="racer@example.com", password="pass")
        grant_tenant_access(target, t1)

        def admit_under_lock():
            Workspace.objects.select_for_update().get(pk=ws.pk)
            WorkspaceMembership.objects.create(workspace=ws, user=target, role=WorkspaceRole.READ)

        with row_locked(admit_under_lock, release_after=0.5), pytest.raises(MembersLackTenant):
            add_tenant_covered_by_members(ws, t2)

        assert not WorkspaceTenant.objects.filter(workspace=ws, tenant=t2).exists()


@pytest.mark.django_db
def test_rediscovery_for_another_member_never_renews_their_tokens(user):
    """A manager's source add rediscovers other members with their tokens as-is.
    Renewing someone else's token could record a refresh failure on a transient
    provider error and take away access they have now."""
    live = ocs_team_connection(user, "team-a")
    expired = ocs_team_connection(user, "team-b")
    expired.social_account.socialtoken_set.update(expires_at=timezone.now() - timedelta(minutes=1))

    with patch.object(workspace_views, "aiter_fresh_access_tokens") as renew:
        pairs = async_to_sync(workspace_views._aunexpired_access_tokens)(user, "ocs")

    renew.assert_not_called()
    assert [account.pk for account, _token in pairs] == [live.social_account_id]
