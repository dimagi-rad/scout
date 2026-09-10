"""Tests for multi-token OAuth: N team-scoped connections per (user, provider) (#156)."""

from __future__ import annotations

import importlib
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from allauth.socialaccount.models import SocialAccount, SocialApp, SocialToken
from asgiref.sync import sync_to_async
from django.apps import apps as global_apps
from django.contrib.sites.models import Site
from django.test import AsyncClient
from django.utils import timezone

from apps.users.models import Tenant, TenantConnection, TenantMembership
from apps.users.providers.ocs.provider import OCSProvider, team_slug_from_uid
from apps.users.services.credential_resolver import (
    CredentialResolutionError,
    aiter_social_tokens,
    arefresh_connection,
    aresolve_credential,
)
from apps.users.services.tenant_resolution import resolve_ocs_chatbots
from apps.workspaces.access import (
    _ashares_live_tenant,
    acovers_live_tenants,
    covers_live_tenants,
)
from apps.workspaces.models import (
    Workspace,
    WorkspaceMembership,
    WorkspaceRole,
    WorkspaceTenant,
)
from mcp_server.envelope import AUTH_TOKEN_EXPIRED


@pytest.fixture
def site(db):
    """The default Site allauth's SocialApp lookup needs."""
    obj, _ = Site.objects.get_or_create(
        id=1, defaults={"domain": "testserver", "name": "Test Server"}
    )
    return obj


def _mock_httpx(mocker, fake_get):
    client = mocker.MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.get = AsyncMock(side_effect=fake_get)
    mocker.patch("httpx.AsyncClient", return_value=client)
    return client


def _ocs_sessions(results):
    class R:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"results": results, "next": None}

    return R()


def _provider() -> OCSProvider:
    app = SocialApp(provider=OCSProvider.id, name="OCS", client_id="x", secret="x")
    return OCSProvider(request=None, app=app)


class TestTeamQualifiedUid:
    def test_uid_carries_the_team_the_token_is_scoped_to(self):
        uid = _provider().extract_uid({"sub": "42", "team": "acme"})
        assert uid == "42#acme"
        assert team_slug_from_uid(uid) == "acme"

    def test_two_teams_of_one_ocs_user_get_distinct_uids(self):
        provider = _provider()
        assert provider.extract_uid({"sub": "42", "team": "acme"}) != provider.extract_uid(
            {"sub": "42", "team": "globex"}
        )

    def test_no_team_claim_keeps_the_bare_subject(self):
        """An OCS deploy that doesn't emit the claim must behave exactly as before."""
        assert _provider().extract_uid({"sub": "42"}) == "42"
        assert _provider().extract_uid({"sub": "42", "team": "  "}) == "42"
        assert team_slug_from_uid("42") == ""

    def test_missing_subject_still_raises(self):
        with pytest.raises(ValueError, match="Cannot determine UID"):
            _provider().extract_uid({"team": "acme"})


@pytest.mark.django_db
class TestUidQualificationMigration:
    """0010 rewrites bare-``sub`` OCS uids so returning users still match."""

    @staticmethod
    def _module():
        return importlib.import_module(
            "apps.users.migrations.0010_qualify_ocs_social_account_uid_by_team"
        )

    def test_forward_qualifies_an_existing_account_and_backward_restores_it(self, user):
        acct = SocialAccount.objects.create(
            user=user, provider="ocs", uid="42", extra_data={"team": "acme"}
        )
        mod = self._module()

        mod.forward(global_apps, None)
        acct.refresh_from_db()
        assert acct.uid == "42#acme"

        mod.backward(global_apps, None)
        acct.refresh_from_db()
        assert acct.uid == "42"

    def test_forward_is_idempotent_and_leaves_claimless_rows_alone(self, user, other_user):
        qualified = SocialAccount.objects.create(
            user=user, provider="ocs", uid="42#acme", extra_data={"team": "acme"}
        )
        claimless = SocialAccount.objects.create(
            user=other_user, provider="ocs", uid="99", extra_data={}
        )
        mod = self._module()

        mod.forward(global_apps, None)
        mod.forward(global_apps, None)

        qualified.refresh_from_db()
        claimless.refresh_from_db()
        assert qualified.uid == "42#acme"
        assert claimless.uid == "99"


@pytest.mark.django_db
class TestScopeBackfillMigration:
    """0011 points existing OAuth connections at their identity and team."""

    @staticmethod
    def _module():
        return importlib.import_module(
            "apps.users.migrations.0011_scope_tenant_connection_to_a_team"
        )

    def test_backfill_records_the_team_an_existing_ocs_token_is_scoped_to(self, user):
        acct = SocialAccount.objects.create(
            user=user, provider="ocs", uid="42#acme", extra_data={"team": "acme"}
        )
        conn = TenantConnection.objects.create(
            user=user, provider="ocs", credential_type=TenantConnection.OAUTH
        )
        tenant = Tenant.objects.create(provider="ocs", external_id="e1", canonical_name="Bot")
        TenantMembership.objects.create(
            user=user, tenant=tenant, connection=conn, team_slug="acme", team_name="Acme Health"
        )

        self._module().forward(global_apps, None)

        conn.refresh_from_db()
        assert conn.scope_key == "acme"
        assert conn.scope_label == "Acme Health"
        assert conn.social_account_id == acct.id

    def test_backfill_leaves_account_wide_providers_unscoped(self, user):
        acct = SocialAccount.objects.create(user=user, provider="commcare", uid="u1")
        conn = TenantConnection.objects.create(
            user=user, provider="commcare", credential_type=TenantConnection.OAUTH
        )

        self._module().forward(global_apps, None)

        conn.refresh_from_db()
        assert conn.scope_key == ""
        assert conn.social_account_id == acct.id

    def test_backfill_falls_back_to_the_team_claim_when_the_uid_is_unqualified(self, user):
        SocialAccount.objects.create(
            user=user, provider="ocs", uid="42", extra_data={"team": "globex"}
        )
        conn = TenantConnection.objects.create(
            user=user, provider="ocs", credential_type=TenantConnection.OAUTH
        )

        self._module().forward(global_apps, None)

        conn.refresh_from_db()
        assert conn.scope_key == "globex"
        assert conn.scope_label == "globex"

    def test_backfill_tolerates_a_connection_with_no_social_account(self, user):
        conn = TenantConnection.objects.create(
            user=user, provider="ocs", credential_type=TenantConnection.OAUTH
        )

        self._module().forward(global_apps, None)

        conn.refresh_from_db()
        assert conn.scope_key == ""
        assert conn.social_account_id is None


@pytest.mark.parametrize(
    "migration_name",
    [
        "0010_qualify_ocs_social_account_uid_by_team",
        "0011_scope_tenant_connection_to_a_team",
    ],
)
def test_every_operation_is_reversible(migration_name):
    """No operation may be irreversible — a ``RunPython`` with no reverse fails here.

    Asserted structurally rather than by running ``migrate`` backwards, which
    would rebuild the shared test schema out from under the rest of the session.
    The data round-trip itself is covered by the 0010 forward/backward test above.
    """
    migration = importlib.import_module(f"apps.users.migrations.{migration_name}").Migration
    irreversible = [
        type(op).__name__ for op in migration("x", "users").operations if not op.reversible
    ]
    assert not irreversible


async def _aocs_identity(user, *, team, token, secret="", expires_in_hours=5, app=None):
    """One team-scoped OCS identity: allauth account + token + a scoped connection."""
    account = await SocialAccount.objects.acreate(
        user=user, provider="ocs", uid=f"42#{team}", extra_data={"sub": "42", "team": team}
    )
    await SocialToken.objects.acreate(
        account=account,
        app=app,
        token=token,
        token_secret=secret,
        expires_at=timezone.now() + timedelta(hours=expires_in_hours),
    )
    conn = await TenantConnection.objects.acreate(
        user=user,
        provider="ocs",
        credential_type=TenantConnection.OAUTH,
        scope_key=team,
        scope_label=team.title(),
        social_account=account,
    )
    return account, conn


async def _achatbot(user, conn, *, team, external_id):
    tenant = await Tenant.objects.acreate(
        provider="ocs", external_id=external_id, canonical_name=external_id
    )
    return await TenantMembership.objects.acreate(
        user=user, tenant=tenant, connection=conn, team_slug=team, team_name=team.title()
    )


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_each_team_resolves_its_own_token(user):
    """The point of the whole change: two teams, two tokens, no ordering guesswork.

    Under the old provider-wide ``.afirst()`` both memberships would have
    resolved to whichever token the ORM returned first.
    """
    _, conn_a = await _aocs_identity(user, team="acme", token="tok-acme")
    _, conn_b = await _aocs_identity(user, team="globex", token="tok-globex")
    tm_a = await _achatbot(user, conn_a, team="acme", external_id="bot-a")
    tm_b = await _achatbot(user, conn_b, team="globex", external_id="bot-b")

    tm_a = await TenantMembership.objects.select_related("connection").aget(id=tm_a.id)
    tm_b = await TenantMembership.objects.select_related("connection").aget(id=tm_b.id)

    assert await aresolve_credential(tm_a) == {"type": "oauth", "value": "tok-acme"}
    assert await aresolve_credential(tm_b) == {"type": "oauth", "value": "tok-globex"}


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_a_chatbot_of_another_team_still_fails_closed(user):
    """A membership left pointing at the wrong team's connection must not resolve."""
    _, conn_a = await _aocs_identity(user, team="acme", token="tok-acme")
    tm = await _achatbot(user, conn_a, team="globex", external_id="bot-b")
    tm = await TenantMembership.objects.select_related("connection").aget(id=tm.id)

    with pytest.raises(CredentialResolutionError) as exc:
        await aresolve_credential(tm)
    assert exc.value.code == AUTH_TOKEN_EXPIRED
    assert "globex" in str(exc.value)


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_refresh_is_per_connection(user, mocker):
    """Renewing one team's near-expiry token must not touch the other team's."""
    app = await SocialApp.objects.acreate(provider="ocs", name="OCS", client_id="cid", secret="sec")
    _, conn_a = await _aocs_identity(
        user, team="acme", token="stale-a", secret="refresh-a", expires_in_hours=-1, app=app
    )
    _, conn_b = await _aocs_identity(
        user, team="globex", token="fresh-b", secret="refresh-b", expires_in_hours=5, app=app
    )
    refresh = mocker.patch(
        "apps.users.services.credential_resolver.refresh_oauth_token",
        new=AsyncMock(return_value="new-a"),
    )

    assert await arefresh_connection(conn_a) == "connected"
    assert await arefresh_connection(conn_b) == "connected"

    assert refresh.await_count == 1
    (refreshed_token, _url), _kwargs = refresh.await_args
    assert refreshed_token.token == "stale-a"
    assert refreshed_token.token_secret == "refresh-a"


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_refresh_reports_expired_when_a_connection_cannot_be_renewed(user):
    _, conn = await _aocs_identity(user, team="acme", token="stale", secret="", expires_in_hours=-1)
    assert await arefresh_connection(conn) == "expired"


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_every_team_token_is_enumerable(user):
    """Callers that must cover all teams get all of them, deterministically ordered."""
    await _aocs_identity(user, team="acme", token="tok-acme")
    await _aocs_identity(user, team="globex", token="tok-globex")

    tokens = await aiter_social_tokens(user, "ocs")

    assert {t.token for t in tokens} == {"tok-acme", "tok-globex"}


# --- the #380 unblock proof --------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_two_team_user_covers_an_all_of_workspace(user, mocker):
    """The reason #156 gates #380: a two-team user now passes a covering-all check.

    #380 will change `_shares_live_tenant` from "shares at least one of the
    workspace's tenants" to "has a live membership for every one of them". That
    was blocked because `unique_oauth_connection_per_user_provider` let a user
    prove only one OCS team, so a workspace spanning two would have denied them
    with no self-remediation — trading a data-exposure bug for a lockout.

    This drives the whole flow for real (two OAuth authorisations, no stubbed
    memberships) and asserts the covering predicate the flip will use. The gate
    itself is deliberately untouched and still any-of; this asserts the flip is
    now *safe*, not that it has happened.
    """
    app = await SocialApp.objects.acreate(provider="ocs", name="OCS", client_id="cid", secret="sec")

    async def _authorize(team, chatbot_id):
        account = await SocialAccount.objects.acreate(
            user=user, provider="ocs", uid=f"42#{team}", extra_data={"sub": "42", "team": team}
        )
        await SocialToken.objects.acreate(
            account=account,
            app=app,
            token=f"tok-{team}",
            expires_at=timezone.now() + timedelta(hours=5),
        )

        async def fake_get(url, headers=None, params=None):
            if "sessions" in url:
                return _ocs_sessions([{"team": {"slug": team, "name": team.title()}}])
            return _ocs_sessions([{"id": chatbot_id, "name": chatbot_id}])

        _mock_httpx(mocker, fake_get)
        return await resolve_ocs_chatbots(user, f"tok-{team}", social_account=account)

    await _authorize("team-a", "bot-a")
    await _authorize("team-b", "bot-b")

    # Two live connections, one per team — the structural limitation is gone.
    assert (
        await TenantConnection.objects.filter(
            user=user, provider="ocs", credential_type=TenantConnection.OAUTH
        ).acount()
        == 2
    )

    tenant_a = await Tenant.objects.aget(provider="ocs", external_id="bot-a")
    tenant_b = await Tenant.objects.aget(provider="ocs", external_id="bot-b")
    # A live membership for each, each backed by its OWN team's connection.
    for tenant, team in ((tenant_a, "team-a"), (tenant_b, "team-b")):
        tm = await TenantMembership.objects.select_related("connection").aget(
            user=user, tenant=tenant
        )
        assert tm.archived_at is None
        assert tm.connection.scope_key == team

    workspace = await Workspace.objects.acreate(name="Both teams", created_by=user)
    await WorkspaceTenant.objects.acreate(workspace=workspace, tenant=tenant_a)
    await WorkspaceTenant.objects.acreate(workspace=workspace, tenant=tenant_b)
    await WorkspaceMembership.objects.acreate(
        workspace=workspace, user=user, role=WorkspaceRole.MANAGE
    )

    tenant_ids = [tenant_a.id, tenant_b.id]

    # THE PROOF: covering-all now passes for a user who holds both teams.
    assert await acovers_live_tenants(user, tenant_ids) is True
    assert await sync_to_async(covers_live_tenants)(user, tenant_ids) is True

    # And it is a real all-of check, not a tautology: losing one tenant fails it
    # while today's any-of gate would still grant access.
    await TenantMembership.objects.filter(user=user, tenant=tenant_b).aupdate(
        archived_at=timezone.now()
    )
    assert await acovers_live_tenants(user, tenant_ids) is False
    assert await _ashares_live_tenant(user, tenant_ids) is True


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_a_single_team_user_still_fails_a_covering_check(user, other_user):
    """The predicate must not be satisfiable by a teammate's membership.

    A credential is never borrowed across users, so one member covering a tenant
    says nothing about another's coverage (#380's standing security decision).
    """
    tenant_a = await Tenant.objects.acreate(provider="ocs", external_id="a", canonical_name="a")
    tenant_b = await Tenant.objects.acreate(provider="ocs", external_id="b", canonical_name="b")
    await TenantMembership.objects.acreate(user=user, tenant=tenant_a)
    await TenantMembership.objects.acreate(user=other_user, tenant=tenant_b)

    assert await acovers_live_tenants(user, [tenant_a.id, tenant_b.id]) is False
    assert await acovers_live_tenants(user, [tenant_a.id]) is True


# --- self-remediation surface ------------------------------------------------


async def _login(user):
    client = AsyncClient()
    await sync_to_async(client.login)(email=user.email, password="testpass123")
    return client


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_connections_endpoint_lists_each_connected_team(user):
    """Without this the user cannot see which teams they hold — half of "no
    self-remediation" would still be unsolved."""
    app = await SocialApp.objects.acreate(provider="ocs", name="OCS", client_id="cid", secret="sec")
    await _aocs_identity(user, team="acme", token="tok-a", app=app)
    # Team B's token is past expiry with no refresh token, so it must report
    # "expired" independently of team A's health.
    await _aocs_identity(user, team="globex", token="tok-b", expires_in_hours=-1, app=app)

    client = await _login(user)
    resp = await client.get("/api/auth/connections/")
    assert resp.status_code == 200

    by_scope = {row["scope_key"]: row for row in resp.json()}
    assert set(by_scope) == {"acme", "globex"}
    assert by_scope["acme"]["scope_label"] == "Acme"
    assert by_scope["acme"]["status"] == "connected"
    assert by_scope["globex"]["status"] == "expired"


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_removing_one_team_leaves_the_other_connected(user):
    app = await SocialApp.objects.acreate(provider="ocs", name="OCS", client_id="cid", secret="sec")
    acct_a, conn_a = await _aocs_identity(user, team="acme", token="tok-a", app=app)
    acct_b, conn_b = await _aocs_identity(user, team="globex", token="tok-b", app=app)
    tm_a = await _achatbot(user, conn_a, team="acme", external_id="bot-a")
    tm_b = await _achatbot(user, conn_b, team="globex", external_id="bot-b")

    client = await _login(user)
    resp = await client.delete(f"/api/auth/connections/{conn_a.id}/")
    assert resp.status_code == 200

    # Team A is gone: its token, connection and chatbot access.
    assert not await SocialToken.objects.filter(account=acct_a).aexists()
    assert not await TenantConnection.objects.filter(id=conn_a.id).aexists()
    assert (await TenantMembership.all_objects.aget(id=tm_a.id)).archived_at is not None

    # Team B is untouched — the whole point of per-connection disconnect.
    assert await SocialToken.objects.filter(account=acct_b).aexists()
    assert await TenantConnection.objects.filter(id=conn_b.id).aexists()
    assert (await TenantMembership.all_objects.aget(id=tm_b.id)).archived_at is None
    # The login identity survives, as it does for provider-wide disconnect.
    assert await SocialAccount.objects.filter(id=acct_a.id).aexists()


@pytest.mark.django_db
def test_providers_view_advertises_a_scoped_provider(user, client, site):
    """The UI needs to know it may offer "connect another team" for OCS."""
    ocs = SocialApp.objects.create(provider="ocs", name="OCS", client_id="c", secret="s")
    ocs.sites.add(site)
    commcare = SocialApp.objects.create(
        provider="commcare", name="CommCare", client_id="c", secret="s"
    )
    commcare.sites.add(site)

    client.force_login(user)
    entries = {p["id"]: p for p in client.get("/api/auth/providers/").json()["providers"]}

    assert entries["ocs"]["supports_multiple_scopes"] is True
    assert entries["commcare"]["supports_multiple_scopes"] is False


@pytest.mark.django_db
def test_one_healthy_team_keeps_the_provider_connected(user, client, site):
    """Provider status was last-row-wins across tokens, so a healthy team could be
    reported expired purely on queryset order once a user held two."""
    app = SocialApp.objects.create(provider="ocs", name="OCS", client_id="c", secret="s")
    app.sites.add(site)
    healthy = SocialAccount.objects.create(user=user, provider="ocs", uid="42#acme")
    SocialToken.objects.create(
        account=healthy, app=app, token="a", expires_at=timezone.now() + timedelta(hours=5)
    )
    dead = SocialAccount.objects.create(user=user, provider="ocs", uid="42#globex")
    SocialToken.objects.create(
        account=dead, app=app, token="b", expires_at=timezone.now() - timedelta(hours=1)
    )

    client.force_login(user)
    entries = {p["id"]: p for p in client.get("/api/auth/providers/").json()["providers"]}

    assert entries["ocs"]["status"] == "connected"
