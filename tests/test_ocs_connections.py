"""Tests for the first-class TenantConnection model and OCS multi-team support."""

from __future__ import annotations

import importlib
import json
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from allauth.socialaccount.models import SocialAccount, SocialApp, SocialToken
from asgiref.sync import sync_to_async
from django.db import IntegrityError, connection
from django.db.migrations.loader import MigrationLoader
from django.test import AsyncClient, Client
from django.utils import timezone

from apps.users.adapters import encrypt_credential
from apps.users.models import Tenant, TenantConnection, TenantMembership
from apps.users.services.api_key_providers.base import TenantDescriptor
from apps.users.services.credential_resolver import (
    CredentialResolutionError,
    aresolve_credential,
)
from apps.users.services.ocs_team import adetect_team_from_api_key
from apps.users.services.tenant_resolution import resolve_ocs_chatbots
from mcp_server.envelope import AUTH_TOKEN_EXPIRED


def _mock_async_client(mocker, fake_get):
    client = mocker.MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.get = AsyncMock(side_effect=fake_get)
    mocker.patch("httpx.AsyncClient", return_value=client)
    return client


def _tenant(ext="exp-1"):
    return Tenant.objects.create(provider="ocs", external_id=ext, canonical_name=ext)


@pytest.mark.django_db
def test_connection_is_credential_only_and_links_memberships(user):
    conn = TenantConnection.objects.create(
        user=user,
        provider="ocs",
        credential_type=TenantConnection.API_KEY,
        encrypted_credential="enc",
    )
    tm = TenantMembership.objects.create(
        user=user, tenant=_tenant(), connection=conn, team_slug="acme", team_name="Acme"
    )
    assert tm.connection_id == conn.id
    assert list(conn.memberships.all()) == [tm]
    assert tm.archived_at is None


@pytest.mark.django_db
def test_one_oauth_connection_per_user_provider_scope(user):
    """Multi-token OAuth (#156) narrowed uniqueness to the scope, not the provider.

    Two teams of one provider are legal; the same team twice is still not.
    """
    TenantConnection.objects.create(
        user=user, provider="ocs", credential_type=TenantConnection.OAUTH, scope_key="acme"
    )
    TenantConnection.objects.create(
        user=user, provider="ocs", credential_type=TenantConnection.OAUTH, scope_key="globex"
    )
    with pytest.raises(IntegrityError):
        TenantConnection.objects.create(
            user=user, provider="ocs", credential_type=TenantConnection.OAUTH, scope_key="acme"
        )


@pytest.mark.django_db
def test_account_wide_providers_still_get_one_oauth_connection(user):
    """``scope_key=""`` is a real value, so the constraint still collapses on it."""
    TenantConnection.objects.create(
        user=user, provider="commcare", credential_type=TenantConnection.OAUTH
    )
    with pytest.raises(IntegrityError):
        TenantConnection.objects.create(
            user=user, provider="commcare", credential_type=TenantConnection.OAUTH
        )


@pytest.mark.django_db
def test_multiple_api_key_connections_allowed(user):
    TenantConnection.objects.create(
        user=user,
        provider="ocs",
        credential_type=TenantConnection.API_KEY,
        encrypted_credential="a",
    )
    TenantConnection.objects.create(
        user=user,
        provider="ocs",
        credential_type=TenantConnection.API_KEY,
        encrypted_credential="b",
    )
    assert TenantConnection.objects.filter(user=user, provider="ocs").count() == 2


@pytest.mark.django_db(transaction=True)
def test_data_migration_maps_legacy_credentials(user):
    """The 0007 data migration maps legacy TenantCredential rows onto connections:
    OAuth collapses to one connection per (user, provider); API keys become one
    connection per credential with ciphertext preserved.

    TenantCredential was dropped by 0008, so we recreate just that one historical
    table (the other tables exist at head and the 0006-state models map to them),
    seed it, run forward(), and drop it again.
    """

    apps06 = MigrationLoader(connection).project_state(("users", "0006_tenant_connections")).apps
    Tenant06 = apps06.get_model("users", "Tenant")
    Mem06 = apps06.get_model("users", "TenantMembership")
    Cred06 = apps06.get_model("users", "TenantCredential")
    Conn06 = apps06.get_model("users", "TenantConnection")

    with connection.cursor() as c:
        c.execute("DROP TABLE IF EXISTS users_tenantcredential CASCADE")
    with connection.schema_editor() as se:
        se.create_model(Cred06)
    try:
        t1 = Tenant06.objects.create(provider="ocs", external_id="e1", canonical_name="e1")
        t2 = Tenant06.objects.create(provider="ocs", external_id="e2", canonical_name="e2")
        t3 = Tenant06.objects.create(provider="commcare", external_id="d1", canonical_name="d1")
        m1 = Mem06.objects.create(user_id=user.id, tenant=t1)
        m2 = Mem06.objects.create(user_id=user.id, tenant=t2)
        m3 = Mem06.objects.create(user_id=user.id, tenant=t3)
        Cred06.objects.create(tenant_membership=m1, credential_type="oauth")
        Cred06.objects.create(tenant_membership=m2, credential_type="oauth")
        Cred06.objects.create(
            tenant_membership=m3, credential_type="api_key", encrypted_credential="enc"
        )

        mod = importlib.import_module(
            "apps.users.migrations.0007_migrate_credentials_to_connections"
        )
        mod.forward(apps06, None)

        # Two OCS OAuth credentials collapse to a single connection.
        ocs_oauth = Conn06.objects.filter(user_id=user.id, provider="ocs", credential_type="oauth")
        assert ocs_oauth.count() == 1
        m1.refresh_from_db()
        m2.refresh_from_db()
        m3.refresh_from_db()
        assert m1.connection_id == m2.connection_id == ocs_oauth.first().id
        # The API key maps to its own connection, ciphertext preserved.
        assert m3.connection.credential_type == "api_key"
        assert m3.connection.encrypted_credential == "enc"
    finally:
        with connection.schema_editor() as se:
            se.delete_model(Cred06)


# --- resolution ------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_resolve_none_when_no_connection(user):
    tenant = await Tenant.objects.acreate(
        provider="ocs", external_id="exp-1", canonical_name="exp-1"
    )
    tm = await TenantMembership.objects.acreate(user=user, tenant=tenant)
    assert await aresolve_credential(tm) is None


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_resolve_api_key(user):
    conn = await TenantConnection.objects.acreate(
        user=user,
        provider="ocs",
        credential_type=TenantConnection.API_KEY,
        encrypted_credential=encrypt_credential("k"),
    )
    tenant = await Tenant.objects.acreate(
        provider="ocs", external_id="exp-1", canonical_name="exp-1"
    )
    tm = await TenantMembership.objects.acreate(
        user=user, tenant=tenant, connection=conn, team_slug="acme", team_name="Acme"
    )
    assert await aresolve_credential(tm) == {"type": "api_key", "value": "k"}


def _mock_token_qs(mocker, *, team, token="tok"):
    account = MagicMock(extra_data={"team": team})
    tok = MagicMock(
        token=token,
        expires_at=timezone.now() + timedelta(hours=5),
        account=account,
        token_secret="",
    )
    qs = MagicMock()
    qs.select_related.return_value = qs
    qs.afirst = AsyncMock(return_value=tok)
    mocker.patch("apps.users.services.credential_resolver._social_token_qs", return_value=qs)


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_resolve_oauth_fails_closed_on_team_mismatch(user, mocker):
    """A team-mismatch must fail closed AND be distinguishable from the
    generic "no credential" case: it raises CredentialResolutionError with the
    AUTH_TOKEN_EXPIRED code and an actionable, re-authorize-oriented message so
    the user is told to re-connect, not just that "no credential is configured"
    (finding 07#3)."""
    conn = await TenantConnection.objects.acreate(
        user=user, provider="ocs", credential_type=TenantConnection.OAUTH
    )
    tenant = await Tenant.objects.acreate(provider="ocs", external_id="x", canonical_name="x")
    tm = await TenantMembership.objects.acreate(
        user=user, tenant=tenant, connection=conn, team_slug="team-a"
    )
    _mock_token_qs(mocker, team="team-b")
    with pytest.raises(CredentialResolutionError) as exc_info:
        await aresolve_credential(tm)
    assert exc_info.value.code == AUTH_TOKEN_EXPIRED
    # Distinct, actionable message — not the generic "No credential configured".
    assert "No credential configured" not in str(exc_info.value)
    assert "team-a" in str(exc_info.value)


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_resolve_oauth_ok_on_team_match(user, mocker):
    conn = await TenantConnection.objects.acreate(
        user=user, provider="ocs", credential_type=TenantConnection.OAUTH
    )
    tenant = await Tenant.objects.acreate(provider="ocs", external_id="y", canonical_name="y")
    tm = await TenantMembership.objects.acreate(
        user=user, tenant=tenant, connection=conn, team_slug="team-a"
    )
    _mock_token_qs(mocker, team="team-a")
    assert await aresolve_credential(tm) == {"type": "oauth", "value": "tok"}


# --- OCS team detection ----------------------------------------------------


def _sessions_response(results):
    class R:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"results": results, "next": None}

    return R()


@pytest.mark.asyncio
async def test_detect_team_from_sessions(mocker):
    async def fake_get(url, headers=None, params=None):
        return _sessions_response([{"team": {"name": "Acme", "slug": "acme"}}])

    _mock_async_client(mocker, fake_get)
    assert await adetect_team_from_api_key("key", "https://ocs.example") == ("acme", "Acme")


@pytest.mark.asyncio
async def test_detect_team_none_when_no_sessions(mocker):
    async def fake_get(url, headers=None, params=None):
        return _sessions_response([])

    _mock_async_client(mocker, fake_get)
    assert await adetect_team_from_api_key("key", "https://ocs.example") is None


# --- OAuth import -----------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_oauth_import_links_team_and_connection(user, mocker):

    await SocialAccount.objects.acreate(
        user=user, provider="ocs", uid="u1", extra_data={"team": "team-a"}
    )
    experiments = [{"id": "exp-1", "name": "Bot 1"}, {"id": "exp-2", "name": "Bot 2"}]

    async def fake_get(url, headers=None, params=None):
        if "sessions" in url:
            return _sessions_response([{"team": {"slug": "team-a", "name": "Team A"}}])
        return _sessions_response(experiments)  # same envelope shape (results/next)

    _mock_async_client(mocker, fake_get)

    await resolve_ocs_chatbots(user, "tok")

    conns = [c async for c in TenantConnection.objects.filter(user=user, provider="ocs")]
    assert len(conns) == 1
    assert conns[0].credential_type == "oauth"
    tms = [
        tm async for tm in TenantMembership.objects.filter(user=user).select_related("connection")
    ]
    assert len(tms) == 2
    assert all(tm.team_slug == "team-a" and tm.team_name == "Team A" for tm in tms)
    assert all(tm.connection_id == conns[0].id for tm in tms)
    assert all(tm.archived_at is None for tm in tms)


# --- connection endpoints ---------------------------------------------------


async def _login(user):

    client = AsyncClient()
    await sync_to_async(client.login)(email=user.email, password="testpass123")
    return client


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_api_key_add_creates_one_connection_with_team(user, mocker):

    mocker.patch(
        "apps.users.services.api_key_providers.ocs.OCSStrategy.verify_and_discover",
        AsyncMock(return_value=[TenantDescriptor("exp-1", "Bot 1")]),
    )
    mocker.patch(
        "apps.users.views.adetect_team_from_api_key", AsyncMock(return_value=("acme", "Acme"))
    )

    client = await _login(user)
    resp = await client.post(
        "/api/auth/connections/",
        data=json.dumps({"provider": "ocs", "fields": {"api_key": "k"}}),
        content_type="application/json",
    )
    assert resp.status_code == 201
    conns = [
        c
        async for c in TenantConnection.objects.filter(
            user=user, provider="ocs", credential_type="api_key"
        )
    ]
    assert len(conns) == 1
    tm = await TenantMembership.objects.select_related("connection").aget(
        user=user, tenant__external_id="exp-1"
    )
    assert tm.team_slug == "acme"
    assert tm.team_name == "Acme"
    assert tm.connection_id == conns[0].id


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_api_key_add_requires_team_name_when_undetectable(user, mocker):

    mocker.patch(
        "apps.users.services.api_key_providers.ocs.OCSStrategy.verify_and_discover",
        AsyncMock(return_value=[TenantDescriptor("exp-1", "Bot 1")]),
    )
    mocker.patch("apps.users.views.adetect_team_from_api_key", AsyncMock(return_value=None))

    client = await _login(user)
    resp = await client.post(
        "/api/auth/connections/",
        data=json.dumps({"provider": "ocs", "fields": {"api_key": "k"}}),
        content_type="application/json",
    )
    assert resp.status_code == 400
    assert not await TenantConnection.objects.filter(user=user).aexists()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_remove_connection_archives_memberships(user):
    conn = await TenantConnection.objects.acreate(
        user=user, provider="ocs", credential_type="api_key", encrypted_credential="e"
    )
    t = await Tenant.objects.acreate(provider="ocs", external_id="exp-9", canonical_name="B")
    tm = await TenantMembership.objects.acreate(
        user=user, tenant=t, connection=conn, team_slug="acme"
    )

    client = await _login(user)
    resp = await client.delete(f"/api/auth/connections/{conn.id}/")
    assert resp.status_code == 200

    tm = await TenantMembership.all_objects.aget(id=tm.id)  # archived: hidden by default manager
    assert tm.archived_at is not None
    assert tm.connection_id is None
    assert not await TenantConnection.objects.filter(id=conn.id).aexists()


# --- the reported bug, end to end -------------------------------------------


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_connecting_a_second_team_no_longer_evicts_the_first(user, mocker):
    """The reported bug's successor: authorising team B must not disown team A.

    Originally this asserted that after re-authorising as team B, team A's
    chatbot failed closed rather than being served the team-B token — correct,
    but the underlying cause was that Scout could only hold one OCS token, so
    switching teams meant losing the first. That is what #156 removed: each team
    now has its own identity, connection and token, and all three credentials
    resolve to their own team. The fail-closed guarantee it protected is pinned
    separately by ``test_a_chatbot_of_another_team_still_fails_closed``.
    """
    app = await SocialApp.objects.acreate(provider="ocs", name="OCS", client_id="cid", secret="sec")

    async def _authorize(team, token_value, chatbot_id):
        """One OAuth round-trip for *team*, as allauth would leave the DB."""
        account = await SocialAccount.objects.acreate(
            user=user, provider="ocs", uid=f"42#{team}", extra_data={"sub": "42", "team": team}
        )
        await SocialToken.objects.acreate(
            account=account,
            app=app,
            token=token_value,
            expires_at=timezone.now() + timedelta(hours=5),
        )

        async def fake_get(url, headers=None, params=None):
            if "sessions" in url:
                return _sessions_response([{"team": {"slug": team, "name": team.title()}}])
            return _sessions_response([{"id": chatbot_id, "name": chatbot_id}])

        _mock_async_client(mocker, fake_get)
        await resolve_ocs_chatbots(user, token_value, social_account=account)

    await _authorize("team-a", "tok-a", "exp-a")
    await _authorize("team-b", "tok-b", "exp-b")

    # Two independent connections, one per team — not one row overwritten twice.
    conns = {
        c.scope_key: c
        async for c in TenantConnection.objects.filter(
            user=user, provider="ocs", credential_type=TenantConnection.OAUTH
        )
    }
    assert set(conns) == {"team-a", "team-b"}

    async def _cred(external_id):
        tm = await TenantMembership.objects.select_related("connection", "user").aget(
            user=user, tenant__external_id=external_id
        )
        return await aresolve_credential(tm)

    # Team A survived team B's arrival, and each team gets its OWN token.
    assert await _cred("exp-a") == {"type": "oauth", "value": "tok-a"}
    assert await _cred("exp-b") == {"type": "oauth", "value": "tok-b"}

    # A third team via API key is still isolated from both.
    conn_c = await TenantConnection.objects.acreate(
        user=user,
        provider="ocs",
        credential_type=TenantConnection.API_KEY,
        encrypted_credential=encrypt_credential("kc"),
    )
    tenant_c = await Tenant.objects.acreate(provider="ocs", external_id="exp-c", canonical_name="C")
    await TenantMembership.objects.acreate(
        user=user, tenant=tenant_c, connection=conn_c, team_slug="team-c", team_name="Team C"
    )
    assert await _cred("exp-c") == {"type": "api_key", "value": "kc"}


# --- archive / restore / multi-key ------------------------------------------


async def _add_ocs_key(client, mocker, *, external_id, name, team_slug, team_name, api_key):

    mocker.patch(
        "apps.users.services.api_key_providers.ocs.OCSStrategy.verify_and_discover",
        AsyncMock(return_value=[TenantDescriptor(external_id, name)]),
    )
    mocker.patch(
        "apps.users.views.adetect_team_from_api_key",
        AsyncMock(return_value=(team_slug, team_name)),
    )
    return await client.post(
        "/api/auth/connections/",
        data=json.dumps({"provider": "ocs", "fields": {"api_key": api_key}}),
        content_type="application/json",
    )


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_readd_unarchives_and_second_key_is_isolated(user, mocker):
    client = await _login(user)

    # Add key for team A (chatbot exp-1), then remove it → membership archived.
    assert (
        await _add_ocs_key(
            client,
            mocker,
            external_id="exp-1",
            name="Bot 1",
            team_slug="team-a",
            team_name="Team A",
            api_key="ka",
        )
    ).status_code == 201
    conn_a = await TenantConnection.objects.aget(user=user, credential_type="api_key")
    await client.delete(f"/api/auth/connections/{conn_a.id}/")
    tm = await TenantMembership.all_objects.aget(user=user, tenant__external_id="exp-1")
    assert tm.archived_at is not None  # archived: hidden by default manager

    # Re-add the same chatbot's key → un-archives and re-links.
    assert (
        await _add_ocs_key(
            client,
            mocker,
            external_id="exp-1",
            name="Bot 1",
            team_slug="team-a",
            team_name="Team A",
            api_key="ka2",
        )
    ).status_code == 201
    tm = await TenantMembership.objects.select_related("connection").aget(
        user=user, tenant__external_id="exp-1"
    )
    assert tm.archived_at is None
    assert tm.connection is not None

    # A second key for a different team → separate connection, doesn't clobber the first.
    assert (
        await _add_ocs_key(
            client,
            mocker,
            external_id="exp-2",
            name="Bot 2",
            team_slug="team-b",
            team_name="Team B",
            api_key="kb",
        )
    ).status_code == 201
    assert await TenantConnection.objects.filter(user=user, credential_type="api_key").acount() == 2
    tm_a = await TenantMembership.objects.select_related("connection").aget(
        user=user, tenant__external_id="exp-1"
    )
    tm_b = await TenantMembership.objects.select_related("connection").aget(
        user=user, tenant__external_id="exp-2"
    )
    assert tm_a.connection_id != tm_b.connection_id


@pytest.mark.django_db
def test_disconnect_archives_oauth_connection(user):
    """Disconnecting an OAuth provider revokes the token, archives its chatbots,
    and removes the OAuth connection."""

    app = SocialApp.objects.create(provider="ocs", name="OCS", client_id="c", secret="s")
    acct = SocialAccount.objects.create(user=user, provider="ocs", uid="u1")
    SocialToken.objects.create(app=app, account=acct, token="t")
    conn = TenantConnection.objects.create(
        user=user, provider="ocs", credential_type=TenantConnection.OAUTH
    )
    tm = TenantMembership.objects.create(
        user=user, tenant=_tenant("exp-1"), connection=conn, team_slug="team-a"
    )

    client = Client()
    client.force_login(user)
    resp = client.post("/api/auth/providers/ocs/disconnect/")
    assert resp.status_code == 200

    tm.refresh_from_db()
    assert tm.archived_at is not None
    assert tm.connection_id is None
    assert not TenantConnection.objects.filter(id=conn.id).exists()
    assert not SocialToken.objects.filter(account=acct).exists()
