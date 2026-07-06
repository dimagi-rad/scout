"""Tests for the WorkspaceInvite model and its conditional unique constraint."""

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.workspaces.models import (
    WorkspaceInvite,
    WorkspaceInviteStatus,
    WorkspaceRole,
    default_invite_expiry,
)


@pytest.mark.django_db
def test_create_invite_defaults(workspace, user):
    invite = WorkspaceInvite.objects.create(
        workspace=workspace,
        email="invitee@example.com",
        role=WorkspaceRole.READ,
        invited_by=user,
    )
    assert invite.status == WorkspaceInviteStatus.PENDING
    assert invite.token is not None
    assert invite.expires_at > timezone.now()
    assert invite.resolved_at is None
    assert invite.resolved_membership is None


@pytest.mark.django_db
def test_is_expired_reads_expires_at(workspace):
    invite = WorkspaceInvite.objects.create(
        workspace=workspace,
        email="invitee@example.com",
        role=WorkspaceRole.READ,
        expires_at=timezone.now() - timezone.timedelta(days=1),
    )
    assert invite.is_expired is True

    fresh = WorkspaceInvite.objects.create(
        workspace=workspace,
        email="fresh@example.com",
        role=WorkspaceRole.READ,
    )
    assert fresh.is_expired is False


@pytest.mark.django_db
def test_default_invite_expiry_is_in_the_future():
    assert default_invite_expiry() > timezone.now()


@pytest.mark.django_db
def test_one_live_invite_per_workspace_email(workspace):
    WorkspaceInvite.objects.create(
        workspace=workspace,
        email="dup@example.com",
        role=WorkspaceRole.READ,
        status=WorkspaceInviteStatus.PENDING,
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        WorkspaceInvite.objects.create(
            workspace=workspace,
            email="dup@example.com",
            role=WorkspaceRole.READ,
            status=WorkspaceInviteStatus.AWAITING_ACCESS,
        )


@pytest.mark.django_db
def test_terminal_invite_does_not_block_a_new_live_one(workspace):
    """A revoked invite is not 'live', so re-inviting the same email is allowed."""
    WorkspaceInvite.objects.create(
        workspace=workspace,
        email="again@example.com",
        role=WorkspaceRole.READ,
        status=WorkspaceInviteStatus.REVOKED,
    )
    # Should not raise.
    WorkspaceInvite.objects.create(
        workspace=workspace,
        email="again@example.com",
        role=WorkspaceRole.READ,
        status=WorkspaceInviteStatus.PENDING,
    )
