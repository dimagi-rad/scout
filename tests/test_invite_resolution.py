"""Tests for the post-login WorkspaceInvite resolver."""

from types import SimpleNamespace

import pytest
from allauth.account.models import EmailAddress
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.users.models import TenantMembership
from apps.users.signals import (
    resolve_pending_invites_on_login,
    resolve_tenant_on_social_login,
)
from apps.workspaces.models import (
    Workspace,
    WorkspaceInvite,
    WorkspaceInviteStatus,
    WorkspaceMembership,
    WorkspaceRole,
)

User = get_user_model()


@pytest.fixture
def invitee(db):
    return User.objects.create_user(email="invitee@example.com", password="pass")


def _invite(workspace, email="invitee@example.com", status=WorkspaceInviteStatus.PENDING, **kw):
    return WorkspaceInvite.objects.create(
        workspace=workspace, email=email, role=WorkspaceRole.READ, status=status, **kw
    )


def _grant_live_tenant(user, tenant):
    TenantMembership.objects.get_or_create(user=user, tenant=tenant)


@pytest.mark.django_db
def test_pending_resolves_to_membership_when_user_has_live_tenant(invitee, workspace, tenant):
    invite = _invite(workspace)
    _grant_live_tenant(invitee, tenant)

    resolve_pending_invites_on_login(invitee)

    invite.refresh_from_db()
    assert invite.status == WorkspaceInviteStatus.ACCEPTED
    assert invite.resolved_at is not None
    membership = WorkspaceMembership.objects.get(workspace=workspace, user=invitee)
    assert membership.role == WorkspaceRole.READ
    assert invite.resolved_membership_id == membership.id


@pytest.mark.django_db
def test_pending_becomes_awaiting_access_without_live_tenant(invitee, workspace):
    invite = _invite(workspace)
    # invitee has no TenantMembership for the workspace's tenant.

    resolve_pending_invites_on_login(invitee)

    invite.refresh_from_db()
    assert invite.status == WorkspaceInviteStatus.AWAITING_ACCESS
    assert not WorkspaceMembership.objects.filter(workspace=workspace, user=invitee).exists()


@pytest.mark.django_db
def test_awaiting_access_resolves_once_tenant_granted(invitee, workspace, tenant):
    invite = _invite(workspace, status=WorkspaceInviteStatus.AWAITING_ACCESS)
    _grant_live_tenant(invitee, tenant)

    resolve_pending_invites_on_login(invitee)

    invite.refresh_from_db()
    assert invite.status == WorkspaceInviteStatus.ACCEPTED
    assert WorkspaceMembership.objects.filter(workspace=workspace, user=invitee).exists()


@pytest.mark.django_db
def test_matches_verified_email_other_than_user_email(invitee, workspace, tenant):
    """Invite addressed to a secondary, verified email still resolves."""
    invite = _invite(workspace, email="secondary@example.com")
    EmailAddress.objects.create(
        user=invitee, email="secondary@example.com", verified=True, primary=False
    )
    _grant_live_tenant(invitee, tenant)

    resolve_pending_invites_on_login(invitee)

    invite.refresh_from_db()
    assert invite.status == WorkspaceInviteStatus.ACCEPTED


@pytest.mark.django_db
def test_does_not_match_unverified_email(invitee, workspace, tenant):
    invite = _invite(workspace, email="unverified@example.com")
    EmailAddress.objects.create(
        user=invitee, email="unverified@example.com", verified=False, primary=False
    )
    _grant_live_tenant(invitee, tenant)

    resolve_pending_invites_on_login(invitee)

    invite.refresh_from_db()
    assert invite.status == WorkspaceInviteStatus.PENDING


@pytest.mark.django_db
def test_expired_invite_is_not_resolved(invitee, workspace, tenant):
    invite = _invite(workspace, expires_at=timezone.now() - timezone.timedelta(days=1))
    _grant_live_tenant(invitee, tenant)

    resolve_pending_invites_on_login(invitee)

    invite.refresh_from_db()
    assert invite.status == WorkspaceInviteStatus.EXPIRED
    assert not WorkspaceMembership.objects.filter(workspace=workspace, user=invitee).exists()


@pytest.mark.django_db
def test_revoked_invite_never_resolves(invitee, workspace, tenant):
    invite = _invite(workspace, status=WorkspaceInviteStatus.REVOKED)
    _grant_live_tenant(invitee, tenant)

    resolve_pending_invites_on_login(invitee)

    invite.refresh_from_db()
    assert invite.status == WorkspaceInviteStatus.REVOKED


@pytest.mark.django_db
def test_zero_tenant_workspace_resolves_on_membership_alone(invitee):
    ws = Workspace.objects.create(name="No tenants")
    invite = _invite(ws)

    resolve_pending_invites_on_login(invitee)

    invite.refresh_from_db()
    assert invite.status == WorkspaceInviteStatus.ACCEPTED
    assert WorkspaceMembership.objects.filter(workspace=ws, user=invitee).exists()


@pytest.mark.django_db
def test_resolver_failure_does_not_break_login(invitee, mocker):
    """A resolver exception must never propagate out of the login signal."""
    mocker.patch(
        "apps.users.signals.resolve_pending_invites_on_login",
        side_effect=RuntimeError("boom"),
    )
    sociallogin = SimpleNamespace(
        user=invitee,
        token=SimpleNamespace(token=""),  # no token -> provider resolution skipped
        account=SimpleNamespace(provider="commcare"),
    )
    # Should not raise.
    resolve_tenant_on_social_login(request=None, sociallogin=sociallogin)
