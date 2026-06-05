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


@pytest.mark.django_db
def test_data_migration_maps_credentials(user):
    """forward() collapses OAuth creds per (user, provider) and links memberships."""
    import importlib

    from apps.users.models import TenantCredential

    t1, t2 = _tenant("exp-1"), _tenant("exp-2")
    tm1 = TenantMembership.objects.create(user=user, tenant=t1)
    tm2 = TenantMembership.objects.create(user=user, tenant=t2)
    TenantCredential.objects.create(tenant_membership=tm1, credential_type="oauth")
    TenantCredential.objects.create(tenant_membership=tm2, credential_type="oauth")

    mod = importlib.import_module("apps.users.migrations.0007_migrate_credentials_to_connections")
    from django.apps import apps as global_apps

    mod.forward(global_apps, None)

    tm1.refresh_from_db()
    tm2.refresh_from_db()
    assert tm1.connection is not None
    assert tm2.connection is not None
    # both OAuth memberships collapse into ONE connection per (user, provider)
    assert tm1.connection_id == tm2.connection_id
    assert tm1.connection.credential_type == "oauth"


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
