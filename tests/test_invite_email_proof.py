"""Workspace invites are claimed only by a user who proved the invited email (R21).

``User.email`` alone is not proof: it can come from a password account or from a
provider that never verified it. Listing, login resolution and the locked
acceptance step all share ``proven_emails``.
"""

from datetime import timedelta

import pytest
from allauth.account.models import EmailAddress
from allauth.socialaccount.models import SocialAccount
from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone

from apps.users import signals
from apps.users.signals import resolve_pending_invites_on_login
from apps.workspaces.models import (
    WorkspaceInvite,
    WorkspaceInviteStatus,
    WorkspaceMembership,
    WorkspaceRole,
)
from apps.workspaces.services.member_coverage import accept_invite_if_covered
from tests.tenant_access import grant_tenant_access

User = get_user_model()

EMAIL = "invitee@example.com"


@pytest.fixture
def invitee(db, tenant):
    """Holds EMAIL as User.email with genuine coverage, but no proof of the email."""
    user = User.objects.create_user(email=EMAIL, password="pass")
    grant_tenant_access(user, tenant)
    return user


def _manage_invite(workspace, status=WorkspaceInviteStatus.PENDING, **kw):
    return WorkspaceInvite.objects.create(
        workspace=workspace, email=EMAIL, role=WorkspaceRole.MANAGE, status=status, **kw
    )


def _assert_not_claimed(invite, workspace, user):
    invite.refresh_from_db()
    assert invite.status != WorkspaceInviteStatus.ACCEPTED
    assert not WorkspaceMembership.objects.filter(workspace=workspace, user=user).exists()


@pytest.mark.django_db
@pytest.mark.parametrize(
    "proof",
    [
        "none",
        "unverified-row",
        "ocs-no-claim",
        "ocs-claim-false",
        "untrusted-provider",
    ],
)
def test_unproven_email_cannot_claim_an_elevated_invite(invitee, workspace, proof):
    if proof == "unverified-row":
        EmailAddress.objects.create(user=invitee, email=EMAIL, verified=False, primary=True)
    elif proof == "ocs-no-claim":
        SocialAccount.objects.create(
            user=invitee, provider="ocs", uid="o#t", extra_data={"email": EMAIL}
        )
    elif proof == "ocs-claim-false":
        SocialAccount.objects.create(
            user=invitee,
            provider="ocs",
            uid="o#t",
            extra_data={"email": EMAIL, "email_verified": False},
        )
    elif proof == "untrusted-provider":
        SocialAccount.objects.create(
            user=invitee, provider="github", uid="gh", extra_data={"email": EMAIL}
        )
    invite = _manage_invite(workspace)

    resolve_pending_invites_on_login(invitee)
    _assert_not_claimed(invite, workspace, invitee)

    assert accept_invite_if_covered(invite, invitee) is None
    _assert_not_claimed(invite, workspace, invitee)


@pytest.mark.django_db
@pytest.mark.parametrize(
    "provider, extra_data",
    [
        ("commcare", {"email": EMAIL}),
        ("commcare_connect", {"email": EMAIL}),
        ("ocs", {"email": EMAIL, "email_verified": True}),
    ],
    ids=["hq", "connect", "ocs-claim-true"],
)
def test_provider_vouched_email_claims_without_an_emailaddress_row(
    invitee, workspace, provider, extra_data
):
    """HQ/Connect identities whose verified row was never persisted still resolve."""
    SocialAccount.objects.create(user=invitee, provider=provider, uid="u", extra_data=extra_data)
    invite = _manage_invite(workspace)

    resolve_pending_invites_on_login(invitee)

    invite.refresh_from_db()
    assert invite.status == WorkspaceInviteStatus.ACCEPTED
    assert WorkspaceMembership.objects.get(workspace=workspace, user=invitee).role == "manage"


@pytest.mark.django_db
def test_acceptance_rechecks_the_email_under_the_lock(workspace, tenant):
    """The service refuses even when a caller skipped the email match."""
    other = User.objects.create_user(email="other@example.com", password="pass")
    EmailAddress.objects.create(user=other, email=other.email, verified=True, primary=True)
    grant_tenant_access(other, tenant)
    invite = _manage_invite(workspace)

    assert accept_invite_if_covered(invite, other) is None
    _assert_not_claimed(invite, workspace, other)


@pytest.mark.django_db
def test_acceptance_rechecks_expiry_under_the_lock(invitee, workspace):
    EmailAddress.objects.create(user=invitee, email=EMAIL, verified=True, primary=True)
    invite = _manage_invite(workspace, expires_at=timezone.now() - timedelta(minutes=1))

    assert accept_invite_if_covered(invite, invitee) is None
    _assert_not_claimed(invite, workspace, invitee)


@pytest.mark.django_db
@pytest.mark.parametrize("verified, listed", [(False, False), (True, True)])
def test_invite_banner_lists_only_proven_emails(invitee, workspace, verified, listed):
    EmailAddress.objects.create(user=invitee, email=EMAIL, verified=verified, primary=True)
    _manage_invite(workspace, status=WorkspaceInviteStatus.AWAITING_ACCESS)
    client = Client()
    client.force_login(invitee)

    body = client.get("/api/invites/").json()

    assert bool(body) is listed


@pytest.mark.django_db
def test_invite_lapsing_before_the_lock_expires_instead_of_awaiting(
    invitee, workspace, monkeypatch, mocker
):
    EmailAddress.objects.create(user=invitee, email=EMAIL, verified=True, primary=True)
    invite = _manage_invite(workspace)
    accept = signals.accept_invite_if_covered

    def lapse_then_accept(stale, who):
        WorkspaceInvite.objects.filter(pk=stale.pk).update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )
        return accept(stale, who)

    monkeypatch.setattr(signals, "accept_invite_if_covered", lapse_then_accept)
    notify = mocker.patch.object(signals, "notify_awaiting_access")

    resolve_pending_invites_on_login(invitee)

    invite.refresh_from_db()
    assert invite.status == WorkspaceInviteStatus.EXPIRED
    notify.assert_not_called()
