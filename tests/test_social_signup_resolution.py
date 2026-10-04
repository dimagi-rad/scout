"""A first-time social sign-in resolves tenants and invites, not just a later login.

allauth auto-signup fires neither ``social_account_added`` (connect only) nor a
``pre_social_login`` for an existing account, so these drive the real pipeline.
"""

import pytest
from allauth.core.context import request_context
from allauth.socialaccount.helpers import complete_social_login
from allauth.socialaccount.models import SocialAccount, SocialApp, SocialToken
from asgiref.sync import sync_to_async
from django.contrib.auth.models import AnonymousUser
from django.contrib.messages.middleware import MessageMiddleware
from django.contrib.sessions.middleware import SessionMiddleware
from django.contrib.sites.models import Site

from apps.users.providers.ocs.provider import OCSProvider
from apps.workspaces.models import WorkspaceInvite, WorkspaceInviteStatus, WorkspaceRole
from tests.tenant_access import grant_tenant_access

INVITEE_EMAIL = "invitee@dimagi.com"


@pytest.fixture
def ocs_app(db):
    site, _ = Site.objects.get_or_create(
        id=1, defaults={"domain": "testserver", "name": "Test Server"}
    )
    app = SocialApp.objects.create(provider=OCSProvider.id, name="OCS", client_id="x", secret="x")
    app.sites.add(site)
    return app


@pytest.fixture
def invite(workspace, user):
    return WorkspaceInvite.objects.create(
        workspace=workspace, email=INVITEE_EMAIL, role=WorkspaceRole.READ, invited_by=user
    )


def _sign_in(rf, ocs_app, sub="ocs-new"):
    request = rf.get("/accounts/ocs/login/callback/")
    SessionMiddleware(lambda request: None).process_request(request)
    MessageMiddleware(lambda request: None).process_request(request)
    request.user = AnonymousUser()
    userinfo = {"sub": sub, "team": "acme", "email": INVITEE_EMAIL, "email_verified": True}
    with request_context(request):
        sociallogin = OCSProvider(request, app=ocs_app).sociallogin_from_response(request, userinfo)
        sociallogin.token = SocialToken(app=ocs_app, token="access-token")
        return complete_social_login(request, sociallogin)


@pytest.fixture
def resolver(mocker, tenant):
    """Stands in for OCS discovery: the sign-in grants access to the invited tenant."""

    async def resolve(user, token, **kwargs):
        await sync_to_async(grant_tenant_access)(user, tenant)
        return []

    return mocker.patch("apps.users.signals.resolve_ocs_chatbots", side_effect=resolve)


@pytest.mark.django_db
def test_first_social_sign_in_resolves_tenants_and_accepts_the_invite(
    rf, ocs_app, invite, resolver, mocker
):
    mocker.patch("apps.users.signals.notify_invite_accepted")

    response = _sign_in(rf, ocs_app)

    assert response.status_code == 302
    account = SocialAccount.objects.get(provider="ocs")
    resolver.assert_called_once()
    assert resolver.call_args.args[0] == account.user
    assert resolver.call_args.kwargs["social_account"] == account
    invite.refresh_from_db()
    assert invite.status == WorkspaceInviteStatus.ACCEPTED
    assert invite.resolved_membership.user == account.user


@pytest.mark.django_db
def test_first_social_sign_in_without_coverage_moves_invite_to_awaiting(
    rf, ocs_app, invite, mocker
):
    resolver = mocker.patch("apps.users.signals.resolve_ocs_chatbots", return_value=[])
    notify = mocker.patch("apps.users.signals.notify_awaiting_access")

    _sign_in(rf, ocs_app)

    resolver.assert_called_once()
    invite.refresh_from_db()
    assert invite.status == WorkspaceInviteStatus.AWAITING_ACCESS
    notify.assert_called_once()


@pytest.mark.django_db
def test_returning_sign_in_resolves_once(rf, ocs_app, invite, resolver, mocker):
    mocker.patch("apps.users.signals.notify_invite_accepted")
    _sign_in(rf, ocs_app)
    resolver.reset_mock()

    _sign_in(rf, ocs_app)

    resolver.assert_called_once()
