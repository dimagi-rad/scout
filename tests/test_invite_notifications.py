"""Tests for WorkspaceInvite notifications (pending email + bidirectional awaiting/accepted)."""

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.users.models import TenantMembership
from apps.users.signals import resolve_pending_invites_on_login
from apps.workspaces.models import (
    Workspace,
    WorkspaceInvite,
    WorkspaceInviteStatus,
    WorkspaceRole,
    WorkspaceTenant,
)
from apps.workspaces.services.invite_notifications import describe_workspace_sources

User = get_user_model()


@pytest.fixture
def client():
    return Client(enforce_csrf_checks=False)


def _deferred_emails(mock_send_email):
    """Return the kwargs of each send_email.defer(...) call."""
    return [call.kwargs for call in mock_send_email.defer.call_args_list]


# ---------------------------------------------------------------------------
# Phase 2: pending invite email
# ---------------------------------------------------------------------------


class TestPendingInviteEmail:
    def test_pending_invite_sends_email_to_invitee(self, client, user, workspace, mocker):
        mock = mocker.patch("apps.workspaces.api.workspace_views.send_pending_invite_email")
        client.force_login(user)
        resp = client.post(
            f"/api/workspaces/{workspace.id}/members/",
            {"email": "ghost@example.com", "role": WorkspaceRole.READ},
            content_type="application/json",
        )
        assert resp.status_code == 201
        mock.assert_called_once()
        invite = mock.call_args.args[0]
        assert invite.email == "ghost@example.com"

    def test_existing_user_without_access_is_notified_at_share_time(
        self, client, user, workspace, mocker
    ):
        """An existing account gets no pending email, so the awaiting_access branch
        must tell them directly (else they'd never learn of the invite)."""
        from apps.workspaces.services import invite_notifications

        mock_task = mocker.patch.object(invite_notifications, "send_email")
        User.objects.create_user(email="noaccess@example.com", password="pass")
        client.force_login(user)
        resp = client.post(
            f"/api/workspaces/{workspace.id}/members/",
            {"email": "noaccess@example.com", "role": WorkspaceRole.READ},
            content_type="application/json",
        )
        assert resp.status_code == 201
        recipients = {r for kw in _deferred_emails(mock_task) for r in kw["recipient_list"]}
        assert "noaccess@example.com" in recipients
        # Manager initiated the action; they should not be emailed here.
        assert user.email not in recipients

    def test_send_pending_invite_email_targets_the_email(self, workspace, user, mocker):
        from apps.workspaces.services import invite_notifications

        mock_task = mocker.patch.object(invite_notifications, "send_email")
        invite = WorkspaceInvite.objects.create(
            workspace=workspace,
            email="ghost@example.com",
            role=WorkspaceRole.READ,
            invited_by=user,
        )
        invite_notifications.send_pending_invite_email(invite)

        sent = _deferred_emails(mock_task)
        assert len(sent) == 1
        assert sent[0]["recipient_list"] == ["ghost@example.com"]
        assert workspace.name in sent[0]["subject"]
        assert str(invite.token) in sent[0]["message"]


# ---------------------------------------------------------------------------
# Phase 3: generic data-source naming
# ---------------------------------------------------------------------------


class TestDescribeWorkspaceSources:
    def _ws_for(self, provider, name):
        from apps.users.models import Tenant

        ws = Workspace.objects.create(name=name)
        tenant = Tenant.objects.create(
            provider=provider, external_id=f"{provider}-1", canonical_name=name
        )
        WorkspaceTenant.objects.create(workspace=ws, tenant=tenant)
        return ws

    @pytest.mark.django_db
    def test_connect_opportunity_phrasing(self):
        ws = self._ws_for("commcare_connect", "Malaria Study")
        assert describe_workspace_sources(ws) == "the CommCare Connect opportunity 'Malaria Study'"

    @pytest.mark.django_db
    def test_ocs_bot_phrasing(self):
        ws = self._ws_for("ocs", "Helper Bot")
        assert describe_workspace_sources(ws) == "the Open Chat Studio bot 'Helper Bot'"

    @pytest.mark.django_db
    def test_commcare_project_phrasing(self):
        ws = self._ws_for("commcare", "Immunization")
        assert describe_workspace_sources(ws) == "the CommCare HQ project 'Immunization'"


# ---------------------------------------------------------------------------
# Phase 3: bidirectional notifications on resolver transitions
# ---------------------------------------------------------------------------


class TestResolverNotifications:
    @pytest.mark.django_db
    def test_awaiting_access_notifies_invitee_and_manager(self, workspace, user, mocker):
        from apps.workspaces.services import invite_notifications

        mock_task = mocker.patch.object(invite_notifications, "send_email")
        invitee = User.objects.create_user(email="invitee@example.com", password="pass")
        WorkspaceInvite.objects.create(
            workspace=workspace,
            email="invitee@example.com",
            role=WorkspaceRole.READ,
            invited_by=user,
            status=WorkspaceInviteStatus.PENDING,
        )
        resolve_pending_invites_on_login(invitee)

        recipients = {r for kw in _deferred_emails(mock_task) for r in kw["recipient_list"]}
        assert "invitee@example.com" in recipients  # invitee
        assert user.email in recipients  # manager

    @pytest.mark.django_db
    def test_accepted_notifies_invitee_and_manager(self, workspace, user, tenant, mocker):
        from apps.workspaces.services import invite_notifications

        mock_task = mocker.patch.object(invite_notifications, "send_email")
        invitee = User.objects.create_user(email="invitee@example.com", password="pass")
        TenantMembership.objects.get_or_create(user=invitee, tenant=tenant)
        WorkspaceInvite.objects.create(
            workspace=workspace,
            email="invitee@example.com",
            role=WorkspaceRole.READ,
            invited_by=user,
            status=WorkspaceInviteStatus.AWAITING_ACCESS,
        )
        resolve_pending_invites_on_login(invitee)

        recipients = {r for kw in _deferred_emails(mock_task) for r in kw["recipient_list"]}
        assert {"invitee@example.com", user.email} <= recipients

    @pytest.mark.django_db
    def test_awaiting_access_is_not_renotified_on_repeat_login(self, workspace, user, mocker):
        from apps.workspaces.services import invite_notifications

        mock_task = mocker.patch.object(invite_notifications, "send_email")
        invitee = User.objects.create_user(email="invitee@example.com", password="pass")
        WorkspaceInvite.objects.create(
            workspace=workspace,
            email="invitee@example.com",
            role=WorkspaceRole.READ,
            invited_by=user,
            status=WorkspaceInviteStatus.AWAITING_ACCESS,
        )
        resolve_pending_invites_on_login(invitee)
        assert mock_task.defer.call_count == 0


# ---------------------------------------------------------------------------
# Phase 3: in-app awaiting_access surface
# ---------------------------------------------------------------------------


class TestMyInvitesEndpoint:
    @pytest.mark.django_db
    def test_returns_current_users_awaiting_access_invites(self, client, workspace, user, tenant):
        invitee = User.objects.create_user(email="invitee@example.com", password="pass")
        WorkspaceInvite.objects.create(
            workspace=workspace,
            email="invitee@example.com",
            role=WorkspaceRole.READ,
            invited_by=user,
            status=WorkspaceInviteStatus.AWAITING_ACCESS,
        )
        client.force_login(invitee)
        resp = client.get("/api/invites/")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 1
        assert body[0]["workspace_name"] == workspace.name
        assert "access" in body[0]["message"].lower()

    @pytest.mark.django_db
    def test_pending_invites_are_not_surfaced_in_app(self, client, workspace, user):
        invitee = User.objects.create_user(email="invitee@example.com", password="pass")
        WorkspaceInvite.objects.create(
            workspace=workspace,
            email="invitee@example.com",
            role=WorkspaceRole.READ,
            status=WorkspaceInviteStatus.PENDING,
        )
        client.force_login(invitee)
        resp = client.get("/api/invites/")
        assert resp.status_code == 200
        assert resp.json() == []
