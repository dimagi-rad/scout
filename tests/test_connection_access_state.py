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


async def _login(user):
    client = AsyncClient()
    await sync_to_async(client.login)(email=user.email, password="testpass123")
    return client


async def _ocs_app():
    app = await SocialApp.objects.acreate(provider="ocs", name="OCS", client_id="c", secret="s")
    site, _ = await Site.objects.aget_or_create(
        id=1, defaults={"domain": "testserver", "name": "test"}
    )
    await app.sites.aadd(site)
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
        credential_type=TenantConnection.OAUTH,
        scope_key=team,
        scope_label=team,
        social_account=account,
    )


async def _bot(user, conn, external_id, *, archived=False):
    tenant = await Tenant.objects.acreate(
        provider="ocs", external_id=external_id, canonical_name=f"Bot {external_id}"
    )
    return await TenantMembership.all_objects.acreate(
        user=user,
        tenant=tenant,
        connection=conn,
        team_slug=conn.scope_key,
        team_name=conn.scope_label,
        archived_at=timezone.now() if archived else None,
    )


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_refused_team_reports_denial_and_lists_its_archived_bots(user):
    app = await _ocs_app()
    conn = await _ocs_team(user, app, team="dimagi-dev")
    denied_at = timezone.now()
    await TenantConnection.objects.filter(pk=conn.pk).aupdate(
        upstream_denial_code=ErrorCode.AUTH_TOKEN_EXPIRED, upstream_denied_at=denied_at
    )
    await _bot(user, conn, "1", archived=True)
    await _bot(user, conn, "2", archived=True)

    client = await _login(user)
    [row] = (await client.get("/api/auth/connections/")).json()

    # The sign-in is healthy, so the 401 is OCS refusing it, not an expiry.
    assert row["status"] == "connected"
    assert row["access_state"] == ACCESS_REFUSED
    assert row["denial_code"] == ErrorCode.AUTH_TOKEN_EXPIRED
    assert row["denied_at"] == denied_at.isoformat()
    assert row["chatbots"] == []
    assert [b["tenant_name"] for b in row["archived_chatbots"]] == ["Bot 1", "Bot 2"]
    assert all(b["archived_at"] for b in row["archived_chatbots"])


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_healthy_team_is_ok_and_partial_team_marks_only_lost_bots(user):
    app = await _ocs_app()
    healthy = await _ocs_team(user, app, team="acme")
    await _bot(user, healthy, "a1")
    partial = await _ocs_team(user, app, team="globex")
    await _bot(user, partial, "g1")
    await _bot(user, partial, "g2", archived=True)

    client = await _login(user)
    by_scope = {r["scope_key"]: r for r in (await client.get("/api/auth/connections/")).json()}

    assert by_scope["acme"]["access_state"] == ACCESS_OK
    assert by_scope["acme"]["denial_code"] is None
    assert by_scope["acme"]["archived_chatbots"] == []
    assert by_scope["globex"]["access_state"] == ACCESS_PARTIAL
    assert [b["tenant_id"] for b in by_scope["globex"]["chatbots"]] == ["g1"]
    assert [b["tenant_id"] for b in by_scope["globex"]["archived_chatbots"]] == ["g2"]


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_expired_sign_in_is_expired_not_refused(user):
    app = await _ocs_app()
    conn = await _ocs_team(user, app, team="acme", expires_in_hours=-1)
    await TenantConnection.objects.filter(pk=conn.pk).aupdate(
        upstream_denial_code=ErrorCode.AUTH_TOKEN_EXPIRED, upstream_denied_at=timezone.now()
    )

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
        user=user,
        provider="ocs",
        credential_type=TenantConnection.API_KEY,
        encrypted_credential="enc",
    )

    client = await _login(user)
    [provider] = (await client.get("/api/auth/providers/")).json()["providers"]
    # API keys belong to no OAuth card.
    assert provider["connection_ids"] == sorted([str(acme.id), str(globex.id)])


@pytest.mark.parametrize(
    ("credential_type", "status", "code", "live", "archived", "expected"),
    [
        (TenantConnection.OAUTH, "connected", "", 2, 0, ACCESS_OK),
        (TenantConnection.OAUTH, "connected", "", 0, 0, ACCESS_OK),
        (TenantConnection.OAUTH, "expired", ErrorCode.AUTH_TOKEN_EXPIRED, 0, 2, ACCESS_EXPIRED),
        (TenantConnection.OAUTH, "connected", ErrorCode.AUTH_TOKEN_EXPIRED, 0, 2, ACCESS_REFUSED),
        (TenantConnection.OAUTH, "unavailable", ErrorCode.AUTH_ACCESS_DENIED, 0, 2, ACCESS_REFUSED),
        (TenantConnection.OAUTH, "connected", "", 0, 2, ACCESS_REFUSED),
        (TenantConnection.OAUTH, "connected", "", 1, 1, ACCESS_PARTIAL),
        (TenantConnection.OAUTH, "needs_team", "", 0, 0, ACCESS_OK),
        (TenantConnection.API_KEY, None, ErrorCode.AUTH_TOKEN_EXPIRED, 0, 2, ACCESS_EXPIRED),
        (TenantConnection.API_KEY, None, ErrorCode.AUTH_ACCESS_DENIED, 0, 2, ACCESS_REFUSED),
    ],
)
def test_connection_access_state(credential_type, status, code, live, archived, expected):
    conn = TenantConnection(
        provider="ocs", credential_type=credential_type, upstream_denial_code=code
    )
    assert (
        connection_access_state(conn, status=status, live_count=live, archived_count=archived)
        == expected
    )
