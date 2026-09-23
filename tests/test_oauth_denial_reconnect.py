"""Exercise allauth's real token-store/signal order when restoring revoked access."""

from unittest.mock import AsyncMock, patch

import pytest
from allauth.core.context import request_context
from allauth.socialaccount.helpers import complete_social_login
from allauth.socialaccount.models import SocialAccount, SocialLogin, SocialToken
from allauth.socialaccount.providers.base import AuthProcess
from django.contrib.auth import login
from django.contrib.auth.models import AnonymousUser
from django.contrib.messages.middleware import MessageMiddleware
from django.contrib.sessions.middleware import SessionMiddleware
from django.utils import timezone

from apps.users.models import Tenant, TenantConnection, TenantMembership, User


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("process", [AuthProcess.LOGIN, AuthProcess.CONNECT])
def test_existing_oauth_login_restores_after_new_token_is_stored(user, httpx_mock, rf, process):
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
    sociallogin.state = {"process": process}
    request = rf.get("/accounts/ocs/login/callback/")
    SessionMiddleware(lambda request: None).process_request(request)
    MessageMiddleware(lambda request: None).process_request(request)
    request.user = AnonymousUser()
    if process == AuthProcess.CONNECT:
        login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    httpx_mock.add_response(json={"results": [{"id": "bot", "name": "Bot"}], "next": None})
    with (
        request_context(request),
        patch(
            "apps.users.services.tenant_resolution.adetect_team_name_from_oauth",
            new=AsyncMock(return_value="Team"),
        ),
    ):
        response = complete_social_login(request, sociallogin)
    assert response.status_code == 302
    assert request.user.pk == user.pk
    assert request.session["_auth_user_id"] == str(user.pk)
    assert httpx_mock.get_request().headers["Authorization"] == "Bearer new"
    assert SocialToken.objects.get(account=account).token == "new"
    assert TenantMembership.objects.filter(pk=tm.pk).exists()
    connection.refresh_from_db()
    assert connection.upstream_denial_code == ""
