"""Lazy discovery must renew expired credentials before deciding access was lost."""

from datetime import timedelta

import httpx
import pytest
from allauth.socialaccount.models import SocialApp
from django.core.cache import cache
from django.utils import timezone

from apps.users.auth_views import _atry_resolve_provider
from apps.users.models import TenantMembership
from apps.users.services.tenant_resolution import resolve_connect_opportunities
from apps.users.services.token_refresh import get_token_url
from apps.users.views import _arefresh_all_identities
from tests.test_upstream_denial import identity


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("caller", ["onboarding", "tenant_list"])
@pytest.mark.parametrize("refresh_status", [200, 400, 503])
async def test_lazy_discovery_preserves_memberships_with_expired_access_token(
    user, httpx_mock, caller, refresh_status
):
    await cache.aclear()
    _account, token, conn, membership = await identity(user)
    token.app = await SocialApp.objects.acreate(
        provider="commcare_connect", client_id="client", secret="secret"
    )
    token.token_secret = "refresh"
    token.expires_at = timezone.now() - timedelta(minutes=1)
    await token.asave()

    def upstream(request):
        if request.url == get_token_url("commcare_connect"):
            payload = (
                {"access_token": "fresh", "expires_in": 3600}
                if refresh_status == 200
                else {"error": "invalid_grant" if refresh_status == 400 else "unavailable"}
            )
            return httpx.Response(refresh_status, json=payload)
        if request.headers.get("Authorization") == "Bearer fresh":
            return httpx.Response(200, json={"opportunities": [{"id": 1, "name": "A"}]})
        return httpx.Response(401)

    httpx_mock.add_callback(upstream, is_reusable=True)
    if caller == "onboarding":
        await _atry_resolve_provider(
            user, "commcare_connect", resolve_connect_opportunities, "Connect"
        )
    else:
        await _arefresh_all_identities(user)

    assert await TenantMembership.objects.filter(pk=membership.pk).aexists()
    await conn.arefresh_from_db()
    assert conn.upstream_denied_at is None
    requests = httpx_mock.get_requests()
    assert str(requests[0].url) == get_token_url("commcare_connect")
    assert len(requests) == (2 if refresh_status == 200 else 1)
    if refresh_status == 200:
        assert requests[1].headers["Authorization"] == "Bearer fresh"
        if caller == "tenant_list":
            await _arefresh_all_identities(user)
            assert len(httpx_mock.get_requests()) == 2
    await cache.aclear()
