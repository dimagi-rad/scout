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
from apps.users.services.tenant_resolution import _sync_memberships
from apps.users.services.upstream_denial import record_validated_upstream_denial

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


async def _bot(user, conn, external_id, *, archived_reason=None):
    tenant = await Tenant.objects.acreate(
        provider="ocs", external_id=external_id, canonical_name=f"Bot {external_id}"
    )
    return await TenantMembership.all_objects.acreate(
        user=user,
        tenant=tenant,
        connection=conn,
        team_slug=conn.scope_key,
        team_name=conn.scope_label,
        archived_at=timezone.now() if archived_reason is not None else None,
        archived_reason=archived_reason or "",
    )


async def _record_denial(conn, *, code, tenant_id=None, at=None):
    fresh = await TenantConnection.objects.aget(pk=conn.pk)
    await sync_to_async(record_validated_upstream_denial)(
        fresh, code=code, tenant_id=tenant_id, now=at
    )


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_refused_team_reports_denial_and_lists_its_archived_bots(user):
    app = await _ocs_app()
    conn = await _ocs_team(user, app, team="dimagi-dev")
    await _bot(user, conn, "1")
    await _bot(user, conn, "2")
    denied_at = timezone.now()
    await _record_denial(conn, code=EXPIRED_CODE, at=denied_at)

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
async def test_partial_team_keeps_every_per_bot_denial_apart_from_unlisted_ones(user):
    app = await _ocs_app()
    healthy = await _ocs_team(user, app, team="acme")
    await _bot(user, healthy, "a1")
    partial = await _ocs_team(user, app, team="globex")
    await _bot(user, partial, "g1")
    first = await _bot(user, partial, "g2")
    second = await _bot(user, partial, "g3")
    await _bot(user, partial, "g4", archived_reason="unlisted")
    # Two single-bot 403s at different times: the second restamps the connection,
    # which must not relabel the first.
    now = timezone.now()
    await _record_denial(
        partial, code=DENIED_CODE, tenant_id=first.tenant_id, at=now - timedelta(days=1)
    )
    await _record_denial(partial, code=DENIED_CODE, tenant_id=second.tenant_id, at=now)

    client = await _login(user)
    by_scope = {r["scope_key"]: r for r in (await client.get("/api/auth/connections/")).json()}

    assert by_scope["acme"]["access_state"] == ACCESS_OK
    assert by_scope["acme"]["denial_code"] is None
    assert by_scope["acme"]["archived_chatbots"] == []
    globex = by_scope["globex"]
    assert globex["access_state"] == ACCESS_PARTIAL
    # A per-bot denial records no connection-wide code.
    assert globex["denied_at"] is None
    assert [b["tenant_id"] for b in globex["chatbots"]] == ["g1"]
    assert {b["tenant_id"]: b["archived_reason"] for b in globex["archived_chatbots"]} == {
        "g2": "denied",
        "g3": "denied",
        "g4": "unlisted",
    }


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_bots_dropped_from_the_listing_alone_raise_no_alarm(user):
    """A deleted bot is archived on the next sync; nobody can restore it, so the
    connection must not keep telling the user to ask an admin."""
    app = await _ocs_app()
    conn = await _ocs_team(user, app, team="acme")
    await _bot(user, conn, "kept")
    await _bot(user, conn, "gone")
    kept = await Tenant.objects.aget(external_id="kept")

    await _sync_memberships(
        user, await TenantConnection.objects.aget(pk=conn.pk), [kept], archive_team_slug="acme"
    )

    client = await _login(user)
    [row] = (await client.get("/api/auth/connections/")).json()
    assert row["access_state"] == ACCESS_OK
    assert [(b["tenant_id"], b["archived_reason"]) for b in row["archived_chatbots"]] == [
        ("gone", "unlisted")
    ]


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_recovery_relabels_a_denied_bot_the_listing_no_longer_has(user):
    """The team is denied, the user is re-added, and one bot was deleted meanwhile:
    the recovered listing restores the rest and the deleted one stops reading denied."""
    app = await _ocs_app()
    conn = await _ocs_team(user, app, team="acme")
    await _bot(user, conn, "kept")
    await _bot(user, conn, "deleted")
    await _record_denial(conn, code=EXPIRED_CODE)
    kept = await Tenant.objects.aget(external_id="kept")

    await _sync_memberships(
        user, await TenantConnection.objects.aget(pk=conn.pk), [kept], archive_team_slug="acme"
    )

    client = await _login(user)
    [row] = (await client.get("/api/auth/connections/")).json()
    assert row["access_state"] == ACCESS_OK
    assert [b["tenant_id"] for b in row["chatbots"]] == ["kept"]
    assert [(b["tenant_id"], b["archived_reason"]) for b in row["archived_chatbots"]] == [
        ("deleted", "unlisted")
    ]


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_refresh_keeps_a_per_bot_denial_the_listing_still_omits(user):
    """Omission is how OCS withholds a bot it denied, so clicking Refresh sources
    before an admin acts must not turn the warning into "no longer listed"."""
    app = await _ocs_app()
    conn = await _ocs_team(user, app, team="acme")
    await _bot(user, conn, "kept")
    denied = await _bot(user, conn, "denied")
    await _record_denial(conn, code=DENIED_CODE, tenant_id=denied.tenant_id)
    kept = await Tenant.objects.aget(external_id="kept")

    await _sync_memberships(
        user, await TenantConnection.objects.aget(pk=conn.pk), [kept], archive_team_slug="acme"
    )

    client = await _login(user)
    [row] = (await client.get("/api/auth/connections/")).json()
    assert row["access_state"] == ACCESS_PARTIAL
    assert [(b["tenant_id"], b["archived_reason"]) for b in row["archived_chatbots"]] == [
        ("denied", "denied")
    ]


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_connection_without_an_identity_still_lands_on_its_card(user):
    """Legacy rows can have no social_account; the card (and its Reconnect) must still
    cover them."""
    await _ocs_app()
    conn = await TenantConnection.objects.acreate(
        user=user, provider="ocs", credential_type=OAUTH, scope_key="acme"
    )
    await SocialAccount.objects.acreate(user=user, provider="ocs", uid="42#other")

    client = await _login(user)
    [provider] = (await client.get("/api/auth/providers/")).json()["providers"]
    assert provider["connection_ids"] == [str(conn.id)]


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_expired_sign_in_is_expired_not_refused(user):
    app = await _ocs_app()
    conn = await _ocs_team(user, app, team="acme", expires_in_hours=-1)
    await _record_denial(conn, code=EXPIRED_CODE)

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
