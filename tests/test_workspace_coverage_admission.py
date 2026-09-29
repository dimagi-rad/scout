"""Membership and source changes keep every member covering every tenant (#381).

The read gate denies a partially covering member; these tests pin that such a
member is never created in the first place: adding a source another member
cannot use is refused atomically with who/what guidance, direct add and invite
acceptance admit only full coverage (otherwise the invite awaits access), and
workspace creation validates every requested source.
"""

import asyncio
import contextlib
import threading
import time
from datetime import timedelta
from unittest.mock import patch

import httpx
import pytest
from allauth.socialaccount.models import SocialAccount, SocialApp, SocialToken
from asgiref.sync import async_to_sync, sync_to_async
from django.contrib.auth import get_user_model
from django.db import connection
from django.utils import timezone
from rest_framework.test import APIClient

from apps.users import signals
from apps.users.models import Tenant, TenantConnection, TenantMembership
from apps.users.services.tenant_resolution import COMMCARE_DOMAIN_API
from apps.users.services.token_refresh import INTERACTIVE_DB_DEADLINE, get_token_url
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
from tests.row_locks import LOCK_SAFETY_SECONDS, row_locked, user_row
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
    return workspace_views.Rediscovery()


def _oauth_identity(
    user, provider="commcare", *, token="tok-stored", refresh="refresh", expires_at=None
) -> TenantConnection:
    """An account-wide OAuth identity as sign-in leaves it: account, token, connection."""
    app, _ = SocialApp.objects.get_or_create(
        provider=provider, name=provider, defaults={"client_id": "cid", "secret": "sec"}
    )
    account = SocialAccount.objects.create(
        user=user, provider=provider, uid=f"{provider}-{user.pk}"
    )
    SocialToken.objects.create(
        account=account, app=app, token=token, token_secret=refresh, expires_at=expires_at
    )
    return TenantConnection.objects.create(
        user=user,
        provider=provider,
        credential_type=TenantConnection.OAUTH,
        scope_key="",
        social_account=account,
    )


def _domains(*tenants):
    return {
        "objects": [
            {"domain_name": t.external_id, "project_name": t.canonical_name} for t in tenants
        ],
        "meta": {"next": None},
    }


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
        assert resp.json()["recheck_complete"] is False
        assert "retrying may help" in resp.json()["error"]

    def test_a_truncated_recheck_is_reported_as_such(self, client, monkeypatch, user, t1, t2):
        ws = _workspace(user, t1)
        _member(ws, "slow@example.com", t1)
        grant_tenant_access(user, t2)
        client.force_login(user)
        monkeypatch.setattr(workspace_views, "MEMBER_REFRESH_BUDGET", 0.01)

        async def _slow(*_args, **_kwargs):
            await asyncio.sleep(1)

        with patch(REFRESH, side_effect=_slow):
            resp = self._post(client, ws, t2)

        assert resp.status_code == 409
        assert resp.json()["recheck_complete"] is False
        assert "retrying may help" in resp.json()["error"]

    def test_a_bystanders_rejected_sign_in_leaves_their_access_alone(
        self, client, user, httpx_mock, t1, t2
    ):
        """G1: rediscovery replays other members' stored tokens. A stale one drawing a
        401 is not this request's to act on: only the member's own sign-in may
        archive their access, so they are asked to sign in and nothing is revoked."""
        ws = _workspace(user, t1)
        grant_tenant_access(user, t2)
        bob = _member(ws, "bob@example.com")
        conn = _oauth_identity(bob)
        TenantMembership.objects.create(user=bob, tenant=t1, connection=conn)
        httpx_mock.add_response(url=COMMCARE_DOMAIN_API, status_code=401)
        client.force_login(user)

        resp = self._post(client, ws, t2)

        assert TenantMembership.objects.filter(user=bob, tenant=t1).exists()
        conn.refresh_from_db()
        assert conn.upstream_denied_at is None
        assert resp.status_code == 409
        body = resp.json()
        assert [(m["email"], m["needs_sign_in"]) for m in body["members"]] == [
            ("bob@example.com", True)
        ]
        assert "sign in to Scout again" in body["error"]
        assert not WorkspaceTenant.objects.filter(workspace=ws, tenant=t2).exists()

    def test_a_bystanders_rediscovery_only_ever_adds_access(self, client, user, httpx_mock, t1, t2):
        """G1: a listing that omits a tenant is the member's own sign-in's to act on."""
        ws = _workspace(user, t1)
        grant_tenant_access(user, t2)
        bob = _member(ws, "bob@example.com")
        conn = _oauth_identity(bob)
        TenantMembership.objects.create(user=bob, tenant=t1, connection=conn)
        httpx_mock.add_response(url=COMMCARE_DOMAIN_API, json=_domains(t2))
        client.force_login(user)

        resp = self._post(client, ws, t2)

        assert resp.status_code == 202
        assert WorkspaceTenant.objects.filter(workspace=ws, tenant=t2).exists()
        assert TenantMembership.objects.filter(user=bob, tenant=t1).exists()
        assert TenantMembership.objects.filter(user=bob, tenant=t2).exists()

    def test_a_provider_error_during_rediscovery_is_reported_as_retryable(
        self, client, user, httpx_mock, t1, t2
    ):
        """G3: resolve errors were swallowed, so a 502 read as a complete recheck."""
        ws = _workspace(user, t1)
        grant_tenant_access(user, t2)
        bob = _member(ws, "bob@example.com", t1)
        _oauth_identity(bob, expires_at=timezone.now() + timedelta(hours=1))
        httpx_mock.add_response(url=COMMCARE_DOMAIN_API, status_code=502)
        client.force_login(user)

        resp = self._post(client, ws, t2)

        assert resp.status_code == 409
        assert resp.json()["recheck_complete"] is False
        assert "retrying may help" in resp.json()["error"]
        assert resp.json()["members"][0]["needs_sign_in"] is False

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
        assert refresh.call_args.kwargs == {}

    def test_member_refreshed_into_coverage_lets_the_add_through(self, client, user, t1, t2):
        ws = _workspace(user, t1)
        brian = _member(ws, "brian@example.com", t1)
        grant_tenant_access(user, t2)
        client.force_login(user)

        async def _grant(target, _providers, **_kwargs):
            await sync_to_async(grant_tenant_access)(target, t2)
            return workspace_views.Rediscovery()

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

    def test_a_zero_tenant_workspace_cannot_gain_a_source(self, client, user, t1):
        """It is denied outright (#381); deleting it is the only way out."""
        ws = _workspace(user)
        grant_tenant_access(user, t1)
        client.force_login(user)

        resp = self._post(client, ws, t1)

        assert resp.status_code == 403
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
        # The named target's own expired tokens are renewed (G2).
        assert refresh.call_args.kwargs == {"renew": True}

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

    def test_an_expired_but_renewable_target_token_is_renewed_and_rediscovered(
        self, client, user, httpx_mock, t1
    ):
        """G2: the named target's own expired token is renewed, so upstream access
        granted since their last sign-in admits them without signing in again."""
        ws = _workspace(user, t1)
        target = User.objects.create_user(email="late@example.com", password="pass")
        _oauth_identity(target, token="tok-old", expires_at=timezone.now() - timedelta(minutes=1))
        httpx_mock.add_response(
            method="POST",
            url=get_token_url("commcare"),
            json={"access_token": "tok-new", "refresh_token": "refresh-2", "expires_in": 900},
        )
        httpx_mock.add_response(
            url=COMMCARE_DOMAIN_API,
            match_headers={"Authorization": "Bearer tok-new"},
            json=_domains(t1),
        )
        client.force_login(user)

        resp = self._add(client, ws, target.email)

        assert resp.status_code == 201
        assert resp.json()["result"] == "member"
        assert SocialToken.objects.get(account__user=target).token == "tok-new"

    def test_a_target_whose_renewal_is_down_is_not_marked_for_reconnect(
        self, client, user, httpx_mock, t1
    ):
        """G2: a transient refresh failure (#596) awaits access without condemning the
        credential; nothing is sent upstream with the expired token."""
        ws = _workspace(user, t1)
        target = User.objects.create_user(email="late@example.com", password="pass")
        conn = _oauth_identity(
            target, token="tok-old", expires_at=timezone.now() - timedelta(minutes=1)
        )
        httpx_mock.add_response(method="POST", url=get_token_url("commcare"), status_code=503)
        client.force_login(user)

        resp = self._add(client, ws, target.email)

        assert resp.json()["result"] == "invite_awaiting_access"
        conn.refresh_from_db()
        assert conn.oauth_refresh_failure_fingerprint == ""
        assert conn.upstream_denied_at is None

    @pytest.mark.parametrize(
        ("status", "error"), [(400, "invalid_grant"), (401, "invalid_client"), (400, "other")]
    )
    def test_a_refused_renewal_marks_nothing_on_the_targets_credential(
        self, client, user, httpx_mock, t1, status, error
    ):
        """G1: the target did not start this request, so a refused renewal must not
        mark their credential for reconnect; that would lock them out of every
        workspace over a manager's click. Their own next sign-in settles it."""
        ws = _workspace(user, t1)
        target = User.objects.create_user(email="late@example.com", password="pass")
        conn = _oauth_identity(
            target, token="tok-old", expires_at=timezone.now() - timedelta(minutes=1)
        )
        httpx_mock.add_response(
            method="POST", url=get_token_url("commcare"), status_code=status, json={"error": error}
        )
        client.force_login(user)

        resp = self._add(client, ws, target.email)

        body = resp.json()
        assert body["result"] == "invite_awaiting_access"
        assert body["recheck_complete"] is True
        assert body["needs_sign_in"] is True
        conn.refresh_from_db()
        assert conn.oauth_refresh_failure_fingerprint == ""
        assert conn.upstream_denied_at is None

    @pytest.mark.parametrize(
        ("expires_in", "rediscovered"),
        [(timedelta(minutes=-1), False), (timedelta(minutes=2), True), (None, True)],
        ids=["expired", "inside-refresh-buffer", "unknown-expiry"],
    )
    def test_an_expired_target_token_that_cannot_be_renewed_asks_them_to_sign_in(
        self, client, user, httpx_mock, t1, expires_in, rediscovered
    ):
        """With no refresh grant, a token admission counts as expired unlocks nothing
        until the target signs in again, even while it still lists their domains; the
        manager must not be told access arrives on its own."""
        ws = _workspace(user, t1)
        target = User.objects.create_user(email="late@example.com", password="pass")
        conn = _oauth_identity(
            target,
            token="tok-old",
            refresh="",
            expires_at=timezone.now() + expires_in if expires_in else None,
        )
        httpx_mock.add_response(url=COMMCARE_DOMAIN_API, json=_domains(t1), is_optional=True)
        client.force_login(user)

        resp = self._add(client, ws, target.email)

        body = resp.json()
        assert body["result"] == "invite_awaiting_access"
        assert body["recheck_complete"] is True
        assert body["needs_sign_in"] is True
        conn.refresh_from_db()
        assert conn.oauth_refresh_failure_fingerprint == ""
        assert conn.upstream_denied_at is None
        assert TenantMembership.objects.filter(user=target, tenant=t1).exists() is rediscovered

    @pytest.mark.parametrize(
        ("refresh", "renewal", "result"),
        [
            # Admission's own gate counts an unrenewable token this close to expiry
            # as expired; rediscovery still records what it can reach.
            ("", None, "invite_awaiting_access"),
            ("refresh", (503, "temporarily_unavailable"), "member"),
            ("refresh", (400, "invalid_grant"), "member"),
        ],
        ids=["cannot-renew", "renewal-down", "renewal-refused"],
    )
    def test_a_target_token_that_is_not_renewed_is_used_while_it_lasts(
        self, client, user, httpx_mock, t1, refresh, renewal, result
    ):
        """G2: a token inside the refresh buffer still works. Failing to renew it
        must not leave the target less covered than using it as it stands would."""
        ws = _workspace(user, t1)
        target = User.objects.create_user(email="late@example.com", password="pass")
        _oauth_identity(
            target,
            token="tok-stored",
            refresh=refresh,
            expires_at=timezone.now() + timedelta(minutes=2),
        )
        if renewal:
            status, error = renewal
            httpx_mock.add_response(
                method="POST",
                url=get_token_url("commcare"),
                status_code=status,
                json={"error": error},
            )
        httpx_mock.add_response(
            url=COMMCARE_DOMAIN_API,
            match_headers={"Authorization": "Bearer tok-stored"},
            json=_domains(t1),
        )
        client.force_login(user)

        resp = self._add(client, ws, target.email)

        assert resp.status_code == 201
        assert TenantMembership.objects.filter(user=target, tenant=t1).exists()
        assert resp.json()["result"] == result

    @pytest.mark.parametrize(
        ("status", "recheck_complete", "needs_sign_in"),
        [(200, True, False), (502, False, False), (401, True, True)],
    )
    def test_an_awaiting_invite_says_whether_retrying_or_signing_in_may_help(
        self, client, user, httpx_mock, t1, status, recheck_complete, needs_sign_in
    ):
        """The manager just typed this person's email: a provider error means retry,
        a refused sign-in means the person must sign in, and neither means they lack
        upstream access."""
        ws = _workspace(user, t1)
        target = User.objects.create_user(email="late@example.com", password="pass")
        _oauth_identity(target, expires_at=timezone.now() + timedelta(hours=1))
        httpx_mock.add_response(
            url=COMMCARE_DOMAIN_API,
            status_code=status,
            json=_domains() if status == 200 else {},
        )
        client.force_login(user)

        resp = self._add(client, ws, target.email)

        assert resp.status_code == 201
        body = resp.json()
        assert body["result"] == "invite_awaiting_access"
        assert body["recheck_complete"] is recheck_complete
        assert body["needs_sign_in"] is needs_sign_in

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

    def test_zero_tenant_workspace_admits_no_one(self, client, user):
        """It is denied outright (#381), so nobody can be added to it."""
        ws = _workspace(user)
        target = User.objects.create_user(email="empty@example.com", password="pass")
        client.force_login(user)

        resp = self._add(client, ws, target.email)

        assert resp.status_code == 403
        assert not ws.memberships.filter(user=target).exists()


@pytest.mark.django_db
class TestRediscoveryOutcome:
    @pytest.mark.parametrize(
        ("provider", "status", "expected"),
        [
            ("commcare", 401, workspace_views.Rediscovery(needs_sign_in=True)),
            ("commcare_connect", 401, workspace_views.Rediscovery(needs_sign_in=True)),
            ("ocs", 401, workspace_views.Rediscovery(needs_sign_in=True)),
            ("commcare", 403, workspace_views.Rediscovery()),
            ("ocs", 403, workspace_views.Rediscovery()),
            # Connect's export list can refuse a user who still holds the opportunity.
            ("commcare_connect", 403, workspace_views.Rediscovery(failed=True)),
        ],
    )
    def test_only_a_refused_sign_in_asks_the_user_to_sign_in(
        self, httpx_mock, provider, status, expected
    ):
        """#372: a 403 is upstream withholding access. Signing in again mints an
        identically scoped token that is refused identically, so only a 401 may send
        the user to sign in. A CommCare or OCS 403 is an authoritative "not covered";
        Connect's is inconclusive, so it reads as a check that couldn't finish."""
        target = User.objects.create_user(email="bob@example.com", password="pass")
        if provider == "ocs":
            ocs_team_connection(target, "team-a")
        else:
            _oauth_identity(target, provider, expires_at=timezone.now() + timedelta(hours=1))
        httpx_mock.add_response(status_code=status, is_reusable=True)

        outcome = async_to_sync(workspace_views._arefresh_target_for_workspace)(
            target, [provider], renew=True
        )

        assert outcome == expected

    def test_a_slow_upstream_cannot_hold_a_direct_add_past_its_budget(
        self, monkeypatch, httpx_mock
    ):
        """The renew pass runs on the sync request thread, so it shares one budget
        rather than spending a full timeout per provider and identity."""
        monkeypatch.setattr(workspace_views, "TARGET_REFRESH_BUDGET", 0.3)
        target = User.objects.create_user(email="bob@example.com", password="pass")
        _oauth_identity(target, expires_at=timezone.now() + timedelta(hours=1))

        async def slow_listing(_request):
            await asyncio.sleep(1)
            return httpx.Response(200, json=_domains())

        httpx_mock.add_callback(slow_listing, url=COMMCARE_DOMAIN_API)
        started = time.monotonic()

        outcome = async_to_sync(workspace_views._arefresh_target_for_workspace)(
            target, ["commcare"], renew=True
        )

        assert time.monotonic() - started < 1
        assert outcome == workspace_views.Rediscovery(failed=True)


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

    def test_an_invite_revoked_mid_login_is_not_accepted(self, user, t1):
        """#561 G4: the login loop read the invite before a manager revoked it."""
        ws = _workspace(user, t1)
        invitee = User.objects.create_user(email="inv@example.com", password="pass")
        grant_tenant_access(invitee, t1)
        stale = self._invite(ws, invitee.email)
        WorkspaceInvite.objects.filter(pk=stale.pk).update(status=WorkspaceInviteStatus.REVOKED)

        assert accept_invite_if_covered(stale, invitee) is None

        assert WorkspaceInvite.objects.get(pk=stale.pk).status == WorkspaceInviteStatus.REVOKED
        assert not WorkspaceMembership.objects.filter(workspace=ws, user=invitee).exists()

    def test_login_does_not_revive_an_invite_revoked_mid_login(
        self, user, t1, t2, monkeypatch, mocker
    ):
        """#561 G4: an uncovered invitee's invite moves to awaiting access only if live."""
        ws = _workspace(user, t1, t2)
        invitee = User.objects.create_user(email="inv@example.com", password="pass")
        grant_tenant_access(invitee, t1)
        invite = self._invite(ws, invitee.email)
        accept = signals.accept_invite_if_covered

        def revoke_then_accept(stale, who):
            WorkspaceInvite.objects.filter(pk=stale.pk).update(status=WorkspaceInviteStatus.REVOKED)
            return accept(stale, who)

        monkeypatch.setattr(signals, "accept_invite_if_covered", revoke_then_accept)
        notify = mocker.patch.object(signals, "notify_awaiting_access")

        resolve_pending_invites_on_login(invitee)

        assert WorkspaceInvite.objects.get(pk=invite.pk).status == WorkspaceInviteStatus.REVOKED
        notify.assert_not_called()

    def test_login_does_not_expire_an_invite_revoked_mid_login(self, user, t1, monkeypatch):
        """#561 G4: a revoke between the login loop's read and its expiry write stands."""
        ws = _workspace(user, t1)
        invitee = User.objects.create_user(email="inv@example.com", password="pass")
        invite = self._invite(ws, invitee.email)
        WorkspaceInvite.objects.filter(pk=invite.pk).update(
            expires_at=timezone.now() - timedelta(days=1)
        )
        is_expired = WorkspaceInvite.is_expired

        def revoke_then_check(stale):
            WorkspaceInvite.objects.filter(pk=stale.pk).update(status=WorkspaceInviteStatus.REVOKED)
            return is_expired.fget(stale)

        monkeypatch.setattr(WorkspaceInvite, "is_expired", property(revoke_then_check))

        resolve_pending_invites_on_login(invitee)

        assert WorkspaceInvite.objects.get(pk=invite.pk).status == WorkspaceInviteStatus.REVOKED

    def test_acceptance_uses_the_role_as_it_stands_under_the_lock(self, user, t1):
        ws = _workspace(user, t1)
        invitee = User.objects.create_user(email="inv@example.com", password="pass")
        grant_tenant_access(invitee, t1)
        stale = self._invite(ws, invitee.email, role=WorkspaceRole.READ_WRITE)
        WorkspaceInvite.objects.filter(pk=stale.pk).update(role=WorkspaceRole.READ)

        membership = accept_invite_if_covered(stale, invitee)

        assert membership.role == WorkspaceRole.READ
        assert WorkspaceInvite.objects.get(pk=stale.pk).role == WorkspaceRole.READ


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


def _release_once_blocked(held):
    """Release the holder only once this thread is waiting on its lock, so the
    test cannot pass without actually exercising the lock wait."""

    def release():
        try:
            held.wait_until_blocking()
        finally:
            connection.close()
            held.set()

    threading.Thread(target=release, daemon=True).start()


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

        with row_locked(add_source_under_lock, release_after=LOCK_SAFETY_SECONDS) as held:
            _release_once_blocked(held)
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

        with (
            row_locked(admit_under_lock, release_after=LOCK_SAFETY_SECONDS) as held,
            pytest.raises(MembersLackTenant),
        ):
            _release_once_blocked(held)
            add_tenant_covered_by_members(ws, t2)

        assert not WorkspaceTenant.objects.filter(workspace=ws, tenant=t2).exists()

    def test_renewing_a_busy_targets_token_keeps_their_rotated_grant(
        self, client, user, httpx_mock, t1
    ):
        """G2: once the provider rotates the target's grant, failing to store it loses
        it for good, and it is the target's grant, not the manager's. A sign-in
        holding the target's row must delay the store, never discard it."""
        ws = _workspace(user, t1)
        target = User.objects.create_user(email="late@example.com", password="pass")
        _oauth_identity(target, token="tok-old", expires_at=timezone.now() - timedelta(minutes=1))
        held = contextlib.ExitStack()

        def rotate_while_target_signs_in(_request):
            held.enter_context(
                row_locked(user_row(target.pk), release_after=INTERACTIVE_DB_DEADLINE + 1)
            )
            return httpx.Response(
                200, json={"access_token": "tok-new", "refresh_token": "refresh-2"}
            )

        httpx_mock.add_callback(
            rotate_while_target_signs_in, method="POST", url=get_token_url("commcare")
        )
        httpx_mock.add_response(
            url=COMMCARE_DOMAIN_API,
            match_headers={"Authorization": "Bearer tok-new"},
            json=_domains(t1),
        )
        client.force_login(user)

        with held:
            resp = client.post(
                f"/api/workspaces/{ws.id}/members/",
                {"email": target.email, "role": "read"},
                format="json",
            )

        assert resp.json()["result"] == "member"
        assert SocialToken.objects.get(account__user=target).token_secret == "refresh-2"


@pytest.mark.django_db
def test_rediscovery_for_another_member_never_renews_their_tokens(user):
    """A manager's source add rediscovers other members with their tokens as-is.
    Renewing someone else's token could record a refresh failure on a transient
    provider error and take away access they have now."""
    live = ocs_team_connection(user, "team-a")
    expired = ocs_team_connection(user, "team-b")
    expired.social_account.socialtoken_set.update(expires_at=timezone.now() - timedelta(minutes=1))

    pairs = async_to_sync(workspace_views._aunexpired_access_tokens)(user, "ocs")

    assert [account.pk for account, _token in pairs] == [live.social_account_id]


@pytest.mark.django_db
def test_rediscovery_uses_a_token_with_no_recorded_expiry(user):
    """Coverage treats an unrecorded expiry as usable, so rediscovery must too, or
    it would skip exactly the member it exists to pick up."""
    unknown = ocs_team_connection(user, "team-a")
    unknown.social_account.socialtoken_set.update(expires_at=None)

    pairs = async_to_sync(workspace_views._aunexpired_access_tokens)(user, "ocs")

    assert [account.pk for account, _token in pairs] == [unknown.social_account_id]
