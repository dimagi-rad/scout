"""Exercise allauth's real token-store/signal order when restoring revoked access."""

from unittest.mock import AsyncMock, patch

import pytest
from allauth.socialaccount.models import SocialAccount, SocialLogin, SocialToken
from allauth.socialaccount.signals import pre_social_login
from django.utils import timezone

from apps.users.models import Tenant, TenantConnection, TenantMembership, User


@pytest.mark.django_db(transaction=True)
def test_existing_oauth_login_restores_after_new_token_is_stored(user, httpx_mock):
    account = SocialAccount.objects.create(user=user, provider="ocs", uid="user#team")
    SocialToken.objects.create(account=account, token="old")
    connection = TenantConnection.objects.create(
        user=user,
        provider="ocs",
        credential_type="oauth",
        scope_key="team",
        social_account=account,
        upstream_denial_code="AUTH_TOKEN_EXPIRED",
        upstream_denied_at=timezone.now(),
    )
    tenant = Tenant.objects.create(provider="ocs", external_id="bot", canonical_name="Bot")
    tm = TenantMembership.all_objects.create(
        user=user, tenant=tenant, connection=connection, archived_at=timezone.now()
    )
    incoming = SocialAccount(provider="ocs", uid="user#team")
    sociallogin = SocialLogin(
        user=User(),
        account=incoming,
        token=SocialToken(token="new", account=incoming),
    )
    httpx_mock.add_response(json={"results": [{"id": "bot", "name": "Bot"}], "next": None})
    with patch(
        "apps.users.services.tenant_resolution.adetect_team_name_from_oauth",
        new=AsyncMock(return_value="Team"),
    ):
        sociallogin.lookup()
        pre_social_login.send(sender=SocialLogin, request=None, sociallogin=sociallogin)
    assert SocialToken.objects.get(account=account).token == "new"
    assert TenantMembership.objects.filter(pk=tm.pk).exists()
    connection.refresh_from_db()
    assert connection.upstream_denial_code == ""
