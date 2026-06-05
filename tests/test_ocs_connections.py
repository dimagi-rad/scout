"""Tests for the first-class TenantConnection model and OCS multi-team support."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from django.db import IntegrityError
from django.utils import timezone

from apps.users.adapters import encrypt_credential
from apps.users.models import Tenant, TenantConnection, TenantMembership
from apps.users.services.credential_resolver import aresolve_credential, resolve_credential
from apps.users.services.ocs_team import adetect_team_from_api_key
from apps.users.services.tenant_resolution import resolve_ocs_chatbots


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
def test_one_oauth_connection_per_user_provider(user):
    TenantConnection.objects.create(
        user=user, provider="ocs", credential_type=TenantConnection.OAUTH
    )
    with pytest.raises(IntegrityError):
        TenantConnection.objects.create(
            user=user, provider="ocs", credential_type=TenantConnection.OAUTH
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


def test_credential_migration_is_well_formed():
    """The credentials->connections data migration is importable with both directions.

    The migration runs against historical models on every test-DB build; its
    forward() collapses OAuth credentials to one connection per (user, provider)
    and copies API-key ciphertext one connection per credential.
    """
    import importlib

    mod = importlib.import_module("apps.users.migrations.0007_migrate_credentials_to_connections")
    assert callable(mod.forward)
    assert callable(mod.reverse)


# --- resolution ------------------------------------------------------------


@pytest.mark.django_db
def test_resolve_none_when_no_connection(user):
    tm = TenantMembership.objects.create(user=user, tenant=_tenant())
    assert resolve_credential(tm) is None


@pytest.mark.django_db
def test_resolve_api_key(user):
    conn = TenantConnection.objects.create(
        user=user,
        provider="ocs",
        credential_type=TenantConnection.API_KEY,
        encrypted_credential=encrypt_credential("k"),
    )
    tm = TenantMembership.objects.create(
        user=user, tenant=_tenant(), connection=conn, team_slug="acme", team_name="Acme"
    )
    assert resolve_credential(tm) == {"type": "api_key", "value": "k"}


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
    conn = await TenantConnection.objects.acreate(
        user=user, provider="ocs", credential_type=TenantConnection.OAUTH
    )
    tenant = await Tenant.objects.acreate(provider="ocs", external_id="x", canonical_name="x")
    tm = await TenantMembership.objects.acreate(
        user=user, tenant=tenant, connection=conn, team_slug="team-a"
    )
    _mock_token_qs(mocker, team="team-b")
    assert await aresolve_credential(tm) is None


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
    from allauth.socialaccount.models import SocialAccount

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
    from asgiref.sync import sync_to_async
    from django.test import AsyncClient

    client = AsyncClient()
    await sync_to_async(client.login)(email=user.email, password="testpass123")
    return client


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_api_key_add_creates_one_connection_with_team(user, mocker):
    import json

    from apps.users.services.api_key_providers.base import TenantDescriptor

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
    import json

    from apps.users.services.api_key_providers.base import TenantDescriptor

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

    tm = await TenantMembership.objects.aget(id=tm.id)
    assert tm.archived_at is not None
    assert tm.connection_id is None
    assert not await TenantConnection.objects.filter(id=conn.id).aexists()
