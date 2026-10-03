"""Connected Accounts says when a connection no longer reaches its sources.

Token health alone showed "Connected" for an OCS team whose every bot was no-access:
OCS answered 401 to a freshly refreshed token, Scout recorded the denial and archived
the memberships, and the page listed the bots as if nothing were wrong.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from allauth.socialaccount.models import SocialAccount, SocialApp, SocialToken
from asgiref.sync import sync_to_async
from django.contrib.sites.models import Site
from django.test import AsyncClient
from django.utils import timezone

from apps.common.error_codes import ErrorCode
from apps.users.models import Tenant, TenantConnection, TenantMembership
from apps.users.services.connection_access import (
    ACCESS_EXPIRED,
    ACCESS_OK,
    ACCESS_PARTIAL,
    ACCESS_REFUSED,
    connection_access_state,
)

OAUTH = TenantConnection.OAUTH
API_KEY = TenantConnection.API_KEY
EXPIRED_CODE = ErrorCode.AUTH_TOKEN_EXPIRED
DENIED_CODE = ErrorCode.AUTH_ACCESS_DENIED


async def _login(user):
    client = AsyncClient()
    await sync_to_async(client.login)(email=user.email, password="testpass123")
    return client


async def _site():
    site, _ = await Site.objects.aget_or_create(
        id=1, defaults={"domain": "testserver", "name": "test"}
    )
    return site


async def _ocs_app():
    app = await SocialApp.objects.acreate(provider="ocs", name="OCS", client_id="c", secret="s")
    await app.sites.aadd(await _site())
    return app


async def _ocs_team(user, app, *, team, expires_in_hours=5):
    account = await SocialAccount.objects.acreate(
        user=user, provider="ocs", uid=f"42#{team}", extra_data={"sub": "42", "team": team}
    )
    await SocialToken.objects.acreate(
        account=account,
        app=app,
        token=f"tok-{team}",
        expires_at=timezone.now() + timedelta(hours=expires_in_hours),
    )
    return await TenantConnection.objects.acreate(
        user=user,
        provider="ocs",
        credential_type=OAUTH,
        scope_key=team,
        scope_label=team,
        social_account=account,
    )


async def _bot(user, conn, external_id, *, archived_at=None):
    tenant = await Tenant.objects.acreate(
        provider="ocs", external_id=external_id, canonical_name=f"Bot {external_id}"
    )
    return await TenantMembership.all_objects.acreate(
        user=user,
        tenant=tenant,
        connection=conn,
        team_slug=conn.scope_key,
        team_name=conn.scope_label,
        archived_at=archived_at,
    )


async def _deny(conn, *, code="", at):
    await TenantConnection.objects.filter(pk=conn.pk).aupdate(
        upstream_denial_code=code, upstream_denied_at=at
    )


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_refused_team_reports_denial_and_lists_its_archived_bots(user):
    app = await _ocs_app()
    conn = await _ocs_team(user, app, team="dimagi-dev")
    denied_at = timezone.now()
    await _deny(conn, code=EXPIRED_CODE, at=denied_at)
    await _bot(user, conn, "1", archived_at=denied_at)
    await _bot(user, conn, "2", archived_at=denied_at)

    client = await _login(user)
    [row] = (await client.get("/api/auth/connections/")).json()

    # The sign-in is healthy, so the 401 is OCS refusing it, not an expiry.
    assert row["status"] == "connected"
    assert row["access_state"] == ACCESS_REFUSED
    assert row["denial_code"] == EXPIRED_CODE
    assert row["denied_at"] == denied_at.isoformat()
    assert row["chatbots"] == []
    assert [b["tenant_name"] for b in row["archived_chatbots"]] == ["Bot 1", "Bot 2"]
    assert {b["archived_reason"] for b in row["archived_chatbots"]} == {"denied"}


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_partial_team_marks_denied_bots_apart_from_unlisted_ones(user):
    app = await _ocs_app()
    healthy = await _ocs_team(user, app, team="acme")
    await _bot(user, healthy, "a1")
    partial = await _ocs_team(user, app, team="globex")
    denied_at = timezone.now()
    # A single-bot 403 stamps the time but leaves the connection's code empty.
    await _deny(partial, at=denied_at)
    await _bot(user, partial, "g1")
    await _bot(user, partial, "g2", archived_at=denied_at)
    await _bot(user, partial, "g3", archived_at=denied_at - timedelta(days=3))

    client = await _login(user)
    by_scope = {r["scope_key"]: r for r in (await client.get("/api/auth/connections/")).json()}

    assert by_scope["acme"]["access_state"] == ACCESS_OK
    assert by_scope["acme"]["denial_code"] is None
    assert by_scope["acme"]["archived_chatbots"] == []
    globex = by_scope["globex"]
    assert globex["access_state"] == ACCESS_PARTIAL
    assert globex["denied_at"] is None
    assert [b["tenant_id"] for b in globex["chatbots"]] == ["g1"]
    assert {b["tenant_id"]: b["archived_reason"] for b in globex["archived_chatbots"]} == {
        "g2": "denied",
        "g3": "unlisted",
    }


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_bots_dropped_from_the_listing_alone_raise_no_alarm(user):
    """A deleted bot is archived on the next sync; nobody can restore it, so the
    connection must not keep telling the user to ask an admin."""
    app = await _ocs_app()
    conn = await _ocs_team(user, app, team="acme")
    await _bot(user, conn, "gone", archived_at=timezone.now())

    client = await _login(user)
    [row] = (await client.get("/api/auth/connections/")).json()
    assert row["access_state"] == ACCESS_OK
    assert row["archived_chatbots"][0]["archived_reason"] == "unlisted"


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_expired_sign_in_is_expired_not_refused(user):
    app = await _ocs_app()
    conn = await _ocs_team(user, app, team="acme", expires_in_hours=-1)
    await _deny(conn, code=EXPIRED_CODE, at=timezone.now())

    client = await _login(user)
    [row] = (await client.get("/api/auth/connections/")).json()
    assert row["status"] == "expired"
    assert row["access_state"] == ACCESS_EXPIRED


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_providers_view_names_the_connections_each_card_covers(user):
    app = await _ocs_app()
    acme = await _ocs_team(user, app, team="acme")
    globex = await _ocs_team(user, app, team="globex")
    await TenantConnection.objects.acreate(
        user=user, provider="ocs", credential_type=API_KEY, encrypted_credential="enc"
    )

    client = await _login(user)
    [provider] = (await client.get("/api/auth/providers/")).json()["providers"]
    # API keys belong to no OAuth card.
    assert provider["connection_ids"] == sorted([str(acme.id), str(globex.id)])


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_commcare_www_and_eu_cards_each_cover_their_own_connection(user):
    """Both servers' connections are provider "commcare"; the card keys on the identity."""
    site = await _site()
    connections = {}
    for app_provider, scope in (("commcare", ""), ("commcare_eu", "eu")):
        app = await SocialApp.objects.acreate(
            provider=app_provider, name=app_provider, client_id="c", secret="s"
        )
        await app.sites.aadd(site)
        account = await SocialAccount.objects.acreate(
            user=user, provider=app_provider, uid=f"u-{app_provider}"
        )
        connections[app_provider] = await TenantConnection.objects.acreate(
            user=user,
            provider="commcare",
            credential_type=OAUTH,
            scope_key=scope,
            social_account=account,
        )

    client = await _login(user)
    providers = (await client.get("/api/auth/providers/")).json()["providers"]
    assert {p["id"]: p["connection_ids"] for p in providers} == {
        "commcare": [str(connections["commcare"].id)],
        "commcare_eu": [str(connections["commcare_eu"].id)],
    }


@pytest.mark.parametrize(
    ("provider", "credential_type", "status", "code", "live", "denied", "expected"),
    [
        ("ocs", OAUTH, "connected", "", 2, 0, ACCESS_OK),
        ("ocs", OAUTH, "connected", "", 0, 0, ACCESS_OK),
        ("ocs", OAUTH, "expired", EXPIRED_CODE, 0, 2, ACCESS_EXPIRED),
        ("ocs", OAUTH, "connected", EXPIRED_CODE, 0, 2, ACCESS_REFUSED),
        ("ocs", OAUTH, "unavailable", DENIED_CODE, 0, 2, ACCESS_REFUSED),
        ("ocs", OAUTH, "connected", "", 0, 2, ACCESS_REFUSED),
        ("ocs", OAUTH, "connected", "", 1, 1, ACCESS_PARTIAL),
        ("ocs", OAUTH, "needs_team", "", 0, 0, ACCESS_OK),
        ("ocs", API_KEY, None, EXPIRED_CODE, 0, 2, ACCESS_EXPIRED),
        ("ocs", API_KEY, None, DENIED_CODE, 0, 2, ACCESS_REFUSED),
        # Account-wide tokens: a 401 is a dead sign-in, which reconnecting fixes.
        ("commcare", OAUTH, "connected", EXPIRED_CODE, 0, 2, ACCESS_EXPIRED),
        ("commcare_connect", OAUTH, "connected", EXPIRED_CODE, 0, 2, ACCESS_EXPIRED),
        ("commcare", OAUTH, "connected", DENIED_CODE, 0, 2, ACCESS_REFUSED),
    ],
)
def test_connection_access_state(provider, credential_type, status, code, live, denied, expected):
    conn = TenantConnection(
        provider=provider, credential_type=credential_type, upstream_denial_code=code
    )
    assert (
        connection_access_state(conn, status=status, live_count=live, denied_count=denied)
        == expected
    )
