"""Tests for WorkspaceInvite notifications (pending email + bidirectional awaiting/accepted)."""

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.users.signals import resolve_pending_invites_on_login
from apps.workspaces.models import (
    Workspace,
    WorkspaceInvite,
    WorkspaceInviteStatus,
    WorkspaceMembership,
    WorkspaceRole,
    WorkspaceTenant,
)
from apps.workspaces.services import invite_notifications
from apps.workspaces.services.invite_notifications import describe_workspace_sources
from tests.tenant_access import grant_tenant_access

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

        mock_task = mocker.patch.object(invite_notifications, "send_email")
        invitee = User.objects.create_user(email="invitee@example.com", password="pass")
        grant_tenant_access(invitee, tenant)
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


class TestDirectAddEmail:
    """#382: a user added straight to a workspace (they already cover its sources)
    gets a notice; before this, the direct-membership branch sent nothing."""

    def _add(self, client, workspace, email):
        return client.post(
            f"/api/workspaces/{workspace.id}/members/",
            {"email": email, "role": WorkspaceRole.READ},
            content_type="application/json",
        )

    def test_direct_add_emails_the_new_member(
        self, client, user, workspace, tenant, mocker, django_capture_on_commit_callbacks
    ):
        mock_task = mocker.patch.object(invite_notifications, "send_email")
        target = User.objects.create_user(email="alice@example.com", password="pass")
        grant_tenant_access(target, tenant)
        client.force_login(user)

        with django_capture_on_commit_callbacks(execute=True):
            resp = self._add(client, workspace, "alice@example.com")

        assert resp.status_code == 201
        assert resp.json()["result"] == "member"
        emails = _deferred_emails(mock_task)
        assert [e["recipient_list"] for e in emails] == [["alice@example.com"]]
        assert workspace.name in emails[0]["subject"]
        assert f"/workspaces/{workspace.id}/chat" in emails[0]["message"]

    def test_email_waits_for_commit(
        self, client, user, workspace, tenant, mocker, django_capture_on_commit_callbacks
    ):
        mock_task = mocker.patch.object(invite_notifications, "send_email")
        target = User.objects.create_user(email="alice@example.com", password="pass")
        grant_tenant_access(target, tenant)
        client.force_login(user)

        with django_capture_on_commit_callbacks(execute=False) as callbacks:
            self._add(client, workspace, "alice@example.com")

        mock_task.defer.assert_not_called()
        assert len(callbacks) == 1

    def test_re_adding_an_existing_member_sends_nothing(
        self, client, user, workspace, tenant, mocker, django_capture_on_commit_callbacks
    ):
        mock_task = mocker.patch.object(invite_notifications, "send_email")
        target = User.objects.create_user(email="alice@example.com", password="pass")
        grant_tenant_access(target, tenant)
        client.force_login(user)
        with django_capture_on_commit_callbacks(execute=True):
            self._add(client, workspace, "alice@example.com")
        mock_task.reset_mock()

        with django_capture_on_commit_callbacks(execute=True):
            resp = self._add(client, workspace, "alice@example.com")

        assert resp.status_code == 409
        mock_task.defer.assert_not_called()

    def test_enqueue_failure_does_not_fail_the_add(
        self, client, user, workspace, tenant, mocker, django_capture_on_commit_callbacks
    ):
        mock_task = mocker.patch.object(invite_notifications, "send_email")
        mock_task.defer.side_effect = RuntimeError("queue down")
        target = User.objects.create_user(email="alice@example.com", password="pass")
        grant_tenant_access(target, tenant)
        client.force_login(user)

        with django_capture_on_commit_callbacks(execute=True):
            resp = self._add(client, workspace, "alice@example.com")

        assert resp.status_code == 201

    def test_direct_add_resolves_a_waiting_invite(
        self, client, user, workspace, tenant, mocker, django_capture_on_commit_callbacks
    ):
        """Left live, the invite would be 'accepted' at next login and send a second,
        contradictory email to the member and the manager."""
        mock_task = mocker.patch.object(invite_notifications, "send_email")
        target = User.objects.create_user(email="alice@example.com", password="pass")
        invite = WorkspaceInvite.objects.create(
            workspace=workspace,
            email="alice@example.com",
            role=WorkspaceRole.READ,
            invited_by=user,
            status=WorkspaceInviteStatus.AWAITING_ACCESS,
        )
        grant_tenant_access(target, tenant)
        client.force_login(user)

        with django_capture_on_commit_callbacks(execute=True):
            assert self._add(client, workspace, "alice@example.com").status_code == 201

        invite.refresh_from_db()
        assert invite.status == WorkspaceInviteStatus.ACCEPTED
        assert invite.resolved_at is not None
        assert invite.resolved_membership == WorkspaceMembership.objects.get(
            workspace=workspace, user=target
        )

        mock_task.reset_mock()
        resolve_pending_invites_on_login(target)
        mock_task.defer.assert_not_called()

    def test_member_added_email_falls_back_for_a_nameless_adder(self, workspace, user, mocker):
        mock_task = mocker.patch.object(invite_notifications, "send_email")
        nameless = User.objects.create_user(email=None, password="pass")
        membership = WorkspaceMembership.objects.get(workspace=workspace, user=user)

        invite_notifications.notify_member_added(membership, nameless)

        message = _deferred_emails(mock_task)[0]["message"]
        assert message.startswith("A Scout workspace manager added you")


class TestRoleChangeEmail:
    """#382: a member whose role a manager changes is told."""

    def _patch(self, client, workspace, membership, role):
        return client.patch(
            f"/api/workspaces/{workspace.id}/members/{membership.id}/",
            {"role": role},
            content_type="application/json",
        )

    def test_role_change_emails_the_member(
        self, client, user, workspace, read_user, mocker, django_capture_on_commit_callbacks
    ):
        mock_task = mocker.patch.object(invite_notifications, "send_email")
        target = WorkspaceMembership.objects.get(workspace=workspace, user=read_user)
        client.force_login(user)

        with django_capture_on_commit_callbacks(execute=True):
            resp = self._patch(client, workspace, target, WorkspaceRole.READ_WRITE)

        assert resp.status_code == 200
        emails = _deferred_emails(mock_task)
        assert [e["recipient_list"] for e in emails] == [["reader@example.com"]]
        assert workspace.name in emails[0]["subject"]
        assert "to Read-Write" in emails[0]["message"]
        assert f"/workspaces/{workspace.id}/chat" in emails[0]["message"]

    def test_email_waits_for_commit(
        self, client, user, workspace, read_user, mocker, django_capture_on_commit_callbacks
    ):
        mock_task = mocker.patch.object(invite_notifications, "send_email")
        target = WorkspaceMembership.objects.get(workspace=workspace, user=read_user)
        client.force_login(user)

        with django_capture_on_commit_callbacks(execute=False) as callbacks:
            self._patch(client, workspace, target, WorkspaceRole.READ_WRITE)

        mock_task.defer.assert_not_called()
        assert len(callbacks) == 1

    def test_same_role_sends_nothing(
        self, client, user, workspace, read_user, mocker, django_capture_on_commit_callbacks
    ):
        mock_task = mocker.patch.object(invite_notifications, "send_email")
        target = WorkspaceMembership.objects.get(workspace=workspace, user=read_user)
        client.force_login(user)

        with django_capture_on_commit_callbacks(execute=True):
            resp = self._patch(client, workspace, target, WorkspaceRole.READ)

        assert resp.status_code == 200
        mock_task.defer.assert_not_called()

    def test_changing_your_own_role_sends_nothing(
        self, client, user, workspace, read_user, mocker, django_capture_on_commit_callbacks
    ):
        mock_task = mocker.patch.object(invite_notifications, "send_email")
        WorkspaceMembership.objects.filter(workspace=workspace, user=read_user).update(
            role=WorkspaceRole.MANAGE
        )
        own = WorkspaceMembership.objects.get(workspace=workspace, user=user)
        client.force_login(user)

        with django_capture_on_commit_callbacks(execute=True):
            resp = self._patch(client, workspace, own, WorkspaceRole.READ)

        assert resp.status_code == 200
        mock_task.defer.assert_not_called()

    def test_email_less_member_is_skipped(self, workspace, user, mocker):
        mock_task = mocker.patch.object(invite_notifications, "send_email")
        nameless = User.objects.create_user(email=None, password="pass")
        membership = WorkspaceMembership.objects.create(
            workspace=workspace, user=nameless, role=WorkspaceRole.READ
        )

        invite_notifications.notify_role_changed(membership, user)

        mock_task.defer.assert_not_called()


class TestMemberRemovedEmail:
    """#382: a member a manager removes is told (their threads go with them)."""

    def _delete(self, client, workspace, membership):
        return client.delete(f"/api/workspaces/{workspace.id}/members/{membership.id}/")

    def test_removal_emails_the_removed_member(
        self, client, user, workspace, read_user, mocker, django_capture_on_commit_callbacks
    ):
        mock_task = mocker.patch.object(invite_notifications, "send_email")
        target = WorkspaceMembership.objects.get(workspace=workspace, user=read_user)
        client.force_login(user)

        with django_capture_on_commit_callbacks(execute=True):
            resp = self._delete(client, workspace, target)

        assert resp.status_code == 204
        emails = _deferred_emails(mock_task)
        assert [e["recipient_list"] for e in emails] == [["reader@example.com"]]
        assert workspace.name in emails[0]["subject"]
        assert emails[0]["message"].startswith("Test User removed you")

    def test_email_waits_for_commit(
        self, client, user, workspace, read_user, mocker, django_capture_on_commit_callbacks
    ):
        mock_task = mocker.patch.object(invite_notifications, "send_email")
        target = WorkspaceMembership.objects.get(workspace=workspace, user=read_user)
        client.force_login(user)

        with django_capture_on_commit_callbacks(execute=False) as callbacks:
            self._delete(client, workspace, target)

        mock_task.defer.assert_not_called()
        assert len(callbacks) == 1

    def test_leaving_sends_nothing(
        self, client, workspace, read_user, mocker, django_capture_on_commit_callbacks
    ):
        mock_task = mocker.patch.object(invite_notifications, "send_email")
        own = WorkspaceMembership.objects.get(workspace=workspace, user=read_user)
        client.force_login(read_user)

        with django_capture_on_commit_callbacks(execute=True):
            resp = self._delete(client, workspace, own)

        assert resp.status_code == 204
        mock_task.defer.assert_not_called()

    def test_refused_removal_sends_nothing(
        self, client, workspace, read_user, write_user, mocker, django_capture_on_commit_callbacks
    ):
        mock_task = mocker.patch.object(invite_notifications, "send_email")
        target = WorkspaceMembership.objects.get(workspace=workspace, user=write_user)
        client.force_login(read_user)

        with django_capture_on_commit_callbacks(execute=True):
            resp = self._delete(client, workspace, target)

        assert resp.status_code == 403
        mock_task.defer.assert_not_called()
