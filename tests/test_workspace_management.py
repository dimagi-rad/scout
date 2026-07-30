"""Tests for workspace management API RBAC invariants (Task 3.1–3.3)."""

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.users.models import TenantMembership
from apps.workspaces.models import (
    Workspace,
    WorkspaceInvite,
    WorkspaceInviteStatus,
    WorkspaceMembership,
    WorkspaceRole,
)

User = get_user_model()


@pytest.fixture
def client():
    return Client(enforce_csrf_checks=False)


@pytest.fixture
def manage_user(db, workspace):
    """The workspace fixture already gives `user` MANAGE role; return that user."""
    return workspace.memberships.get(role=WorkspaceRole.MANAGE).user


@pytest.fixture
def second_tenant(db):
    from apps.users.models import Tenant

    return Tenant.objects.create(
        provider="commcare", external_id="other-domain", canonical_name="Other Domain"
    )


# ---------------------------------------------------------------------------
# Workspace list
# ---------------------------------------------------------------------------


class TestWorkspaceList:
    def test_list_returns_only_users_workspaces(self, client, user, workspace, db):
        other_user = User.objects.create_user(email="other@example.com", password="pass")
        other_ws = Workspace.objects.create(name="Other", created_by=other_user)
        WorkspaceMembership.objects.create(
            workspace=other_ws, user=other_user, role=WorkspaceRole.MANAGE
        )

        client.force_login(user)
        resp = client.get("/api/workspaces/")
        assert resp.status_code == 200
        ids = [w["id"] for w in resp.json()]
        assert str(workspace.id) in ids
        assert str(other_ws.id) not in ids

    def test_list_includes_role_and_tenants(self, client, user, workspace, tenant):
        client.force_login(user)
        resp = client.get("/api/workspaces/")
        assert resp.status_code == 200
        entry = next(w for w in resp.json() if w["id"] == str(workspace.id))
        assert entry["role"] == WorkspaceRole.MANAGE
        assert entry["member_count"] == 1
        assert len(entry["tenants"]) == 1
        assert entry["tenants"][0]["tenant_name"] == tenant.canonical_name
        assert entry["tenants"][0]["provider"] == tenant.provider

    def test_list_requires_authentication(self, client):
        resp = client.get("/api/workspaces/")
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Workspace create
# ---------------------------------------------------------------------------


class TestWorkspaceCreate:
    def test_create_workspace(self, client, user, tenant_membership):
        client.force_login(user)
        resp = client.post(
            "/api/workspaces/",
            {"name": "New workspace", "tenant_ids": [str(tenant_membership.tenant.id)]},
            content_type="application/json",
        )
        assert resp.status_code == 201
        assert resp.json()["name"] == "New workspace"
        assert WorkspaceMembership.objects.filter(
            workspace_id=resp.json()["id"], user=user, role=WorkspaceRole.MANAGE
        ).exists()

    def test_cannot_create_workspace_for_inaccessible_tenant(self, client, user, second_tenant, db):
        client.force_login(user)
        resp = client.post(
            "/api/workspaces/",
            {"name": "Bad", "tenant_ids": [str(second_tenant.id)]},
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_create_requires_name(self, client, user, tenant_membership):
        client.force_login(user)
        resp = client.post(
            "/api/workspaces/",
            {"tenant_ids": [str(tenant_membership.tenant.id)]},
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_create_workspace_with_no_tenants(self, client, user):
        """POST /api/workspaces/ succeeds with tenant_ids=[] (tenants added later)."""
        client.force_login(user)
        resp = client.post(
            "/api/workspaces/",
            {"name": "Empty WS", "tenant_ids": []},
            content_type="application/json",
        )
        assert resp.status_code == 201, resp.json()
        assert resp.json()["name"] == "Empty WS"
        assert resp.json()["tenants"] == []


# ---------------------------------------------------------------------------
# Workspace rename (PATCH)
# ---------------------------------------------------------------------------


class TestWorkspaceRename:
    def test_manager_can_rename(self, client, user, workspace):
        client.force_login(user)
        resp = client.patch(
            f"/api/workspaces/{workspace.id}/",
            {"name": "Renamed"},
            content_type="application/json",
        )
        assert resp.status_code == 200
        workspace.refresh_from_db()
        assert workspace.name == "Renamed"

    def test_non_manager_cannot_rename(self, client, workspace, db):
        write_user = User.objects.create_user(email="w@example.com", password="pass")
        WorkspaceMembership.objects.create(
            workspace=workspace, user=write_user, role=WorkspaceRole.READ_WRITE
        )
        client.force_login(write_user)
        resp = client.patch(
            f"/api/workspaces/{workspace.id}/",
            {"name": "Sneaky rename"},
            content_type="application/json",
        )
        assert resp.status_code == 403

    def test_non_member_gets_403(self, client, workspace, db):
        outsider = User.objects.create_user(email="out@example.com", password="pass")
        client.force_login(outsider)
        resp = client.patch(
            f"/api/workspaces/{workspace.id}/",
            {"name": "Whatever"},
            content_type="application/json",
        )
        assert resp.status_code == 403

    def test_system_prompt_too_long_returns_400(self, client, user, workspace):
        client.force_login(user)
        resp = client.patch(
            f"/api/workspaces/{workspace.id}/",
            {"system_prompt": "x" * 10_001},
            content_type="application/json",
        )
        assert resp.status_code == 400
        assert "system_prompt" in resp.json()["error"]

    def test_system_prompt_at_limit_is_accepted(self, client, user, workspace):
        client.force_login(user)
        resp = client.patch(
            f"/api/workspaces/{workspace.id}/",
            {"system_prompt": "y" * 10_000},
            content_type="application/json",
        )
        assert resp.status_code == 200
        workspace.refresh_from_db()
        assert len(workspace.system_prompt) == 10_000


# ---------------------------------------------------------------------------
# Workspace delete
# ---------------------------------------------------------------------------


class TestWorkspaceDelete:
    def test_cannot_delete_last_workspace_for_tenant(self, client, user, workspace):
        client.force_login(user)
        resp = client.delete(f"/api/workspaces/{workspace.id}/")
        assert resp.status_code == 400
        assert "last workspace" in resp.json()["error"].lower()

    def test_non_manager_cannot_delete(self, client, workspace, db):
        reader = User.objects.create_user(email="r@example.com", password="pass")
        WorkspaceMembership.objects.create(
            workspace=workspace, user=reader, role=WorkspaceRole.READ
        )
        client.force_login(reader)
        resp = client.delete(f"/api/workspaces/{workspace.id}/")
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Member management: last-manager guards
# ---------------------------------------------------------------------------


class TestMemberManagement:
    def test_cannot_demote_last_manager(self, client, user, workspace):
        membership = WorkspaceMembership.objects.get(workspace=workspace, user=user)
        client.force_login(user)
        resp = client.patch(
            f"/api/workspaces/{workspace.id}/members/{membership.id}/",
            {"role": WorkspaceRole.READ_WRITE},
            content_type="application/json",
        )
        assert resp.status_code == 400
        assert "last manager" in resp.json()["error"].lower()

    def test_cannot_remove_last_manager(self, client, user, workspace):
        membership = WorkspaceMembership.objects.get(workspace=workspace, user=user)
        client.force_login(user)
        resp = client.delete(f"/api/workspaces/{workspace.id}/members/{membership.id}/")
        assert resp.status_code == 400
        assert "last manager" in resp.json()["error"].lower()

    def test_second_manager_can_be_demoted(self, client, user, workspace, db):
        second = User.objects.create_user(email="mgr2@example.com", password="pass")
        second_membership = WorkspaceMembership.objects.create(
            workspace=workspace, user=second, role=WorkspaceRole.MANAGE
        )
        client.force_login(user)
        resp = client.patch(
            f"/api/workspaces/{workspace.id}/members/{second_membership.id}/",
            {"role": WorkspaceRole.READ_WRITE},
            content_type="application/json",
        )
        assert resp.status_code == 200
        second_membership.refresh_from_db()
        assert second_membership.role == WorkspaceRole.READ_WRITE

    def test_removing_member_deletes_their_threads(self, client, user, workspace, db):
        from apps.chat.models import Thread

        writer = User.objects.create_user(email="wr@example.com", password="pass")
        writer_membership = WorkspaceMembership.objects.create(
            workspace=workspace, user=writer, role=WorkspaceRole.READ_WRITE
        )
        thread = Thread.objects.create(workspace=workspace, user=writer, title="Writer thread")

        client.force_login(user)
        resp = client.delete(f"/api/workspaces/{workspace.id}/members/{writer_membership.id}/")
        assert resp.status_code == 204
        assert not Thread.objects.filter(id=thread.id).exists()

    def test_read_write_member_cannot_remove_others(self, client, workspace, db):
        writer = User.objects.create_user(email="wr@example.com", password="pass")
        WorkspaceMembership.objects.create(
            workspace=workspace, user=writer, role=WorkspaceRole.READ_WRITE
        )
        reader = User.objects.create_user(email="rd@example.com", password="pass")
        reader_membership = WorkspaceMembership.objects.create(
            workspace=workspace, user=reader, role=WorkspaceRole.READ
        )
        client.force_login(writer)
        resp = client.delete(f"/api/workspaces/{workspace.id}/members/{reader_membership.id}/")
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Member management: add member
# ---------------------------------------------------------------------------


class TestMemberAdd:
    def test_manager_can_add_same_tenant_user(self, client, user, workspace, tenant, db):
        """Manager adds an existing user who shares the workspace's tenant."""
        target = User.objects.create_user(email="alice@example.com", password="pass")
        TenantMembership.objects.create(user=target, tenant=tenant)

        client.force_login(user)
        resp = client.post(
            f"/api/workspaces/{workspace.id}/members/",
            {"email": "alice@example.com", "role": WorkspaceRole.READ_WRITE},
            content_type="application/json",
        )

        assert resp.status_code == 201
        body = resp.json()
        assert body["result"] == "member"
        assert body["email"] == "alice@example.com"
        assert body["role"] == WorkspaceRole.READ_WRITE
        assert body.keys() == {"result", "id", "user_id", "email", "name", "role", "created_at"}
        assert body["user_id"] == str(target.id)
        assert body["name"] == target.get_full_name()

        membership = WorkspaceMembership.objects.get(workspace=workspace, user=target)
        assert membership.invited_by == user
        assert membership.role == WorkspaceRole.READ_WRITE

    def test_missing_email_returns_400(self, client, user, workspace):
        client.force_login(user)
        resp = client.post(
            f"/api/workspaces/{workspace.id}/members/",
            {"role": WorkspaceRole.READ},
            content_type="application/json",
        )
        assert resp.status_code == 400
        assert resp.json()["error"] == "Email is required."

    def test_malformed_email_returns_400(self, client, user, workspace):
        client.force_login(user)
        resp = client.post(
            f"/api/workspaces/{workspace.id}/members/",
            {"email": "not-an-email", "role": WorkspaceRole.READ},
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_invalid_role_returns_400(self, client, user, workspace):
        client.force_login(user)
        resp = client.post(
            f"/api/workspaces/{workspace.id}/members/",
            {"email": "alice@example.com", "role": "admin"},
            content_type="application/json",
        )
        assert resp.status_code == 400
        assert resp.json()["error"] == "Invalid role."

    def test_unknown_email_creates_pending_invite(self, client, user, workspace):
        """No Scout user with that email → an email-keyed pending invite, not a 404."""
        client.force_login(user)
        resp = client.post(
            f"/api/workspaces/{workspace.id}/members/",
            {"email": "ghost@example.com", "role": WorkspaceRole.READ},
            content_type="application/json",
        )
        assert resp.status_code == 201, resp.json()
        body = resp.json()
        assert body["result"] == "invite_pending"
        assert body["email"] == "ghost@example.com"
        assert body["status"] == WorkspaceInviteStatus.PENDING

        invite = WorkspaceInvite.objects.get(workspace=workspace, email="ghost@example.com")
        assert invite.status == WorkspaceInviteStatus.PENDING
        assert invite.invited_by == user
        assert invite.role == WorkspaceRole.READ
        # No placeholder User row is ever created.
        assert not User.objects.filter(email__iexact="ghost@example.com").exists()

    def test_email_is_normalized_lowercase_on_invite(self, client, user, workspace):
        client.force_login(user)
        resp = client.post(
            f"/api/workspaces/{workspace.id}/members/",
            {"email": "MixedCase@Example.com", "role": WorkspaceRole.READ},
            content_type="application/json",
        )
        assert resp.status_code == 201, resp.json()
        assert WorkspaceInvite.objects.filter(email="mixedcase@example.com").exists()

    def test_user_without_shared_tenant_creates_awaiting_access_invite(
        self, client, user, workspace, db
    ):
        """Target exists but has no shared tenant and no token to refresh with →
        an awaiting_access invite that resolves once they gain upstream access."""
        _outsider = User.objects.create_user(email="outsider@example.com", password="pass")
        # Deliberately no TenantMembership and no SocialToken.

        client.force_login(user)
        resp = client.post(
            f"/api/workspaces/{workspace.id}/members/",
            {"email": "outsider@example.com", "role": WorkspaceRole.READ},
            content_type="application/json",
        )
        assert resp.status_code == 201, resp.json()
        body = resp.json()
        assert body["result"] == "invite_awaiting_access"
        assert body["status"] == WorkspaceInviteStatus.AWAITING_ACCESS

        invite = WorkspaceInvite.objects.get(workspace=workspace, email="outsider@example.com")
        assert invite.status == WorkspaceInviteStatus.AWAITING_ACCESS
        # The existing user is NOT added as a member (no live tenant access).
        assert not WorkspaceMembership.objects.filter(workspace=workspace, user=_outsider).exists()

    def test_reinvite_updates_role_and_is_idempotent(self, client, user, workspace):
        client.force_login(user)
        first = client.post(
            f"/api/workspaces/{workspace.id}/members/",
            {"email": "ghost@example.com", "role": WorkspaceRole.READ},
            content_type="application/json",
        )
        assert first.status_code == 201
        second = client.post(
            f"/api/workspaces/{workspace.id}/members/",
            {"email": "ghost@example.com", "role": WorkspaceRole.MANAGE},
            content_type="application/json",
        )
        assert second.status_code == 201
        invites = WorkspaceInvite.objects.filter(workspace=workspace, email="ghost@example.com")
        assert invites.count() == 1
        assert invites.first().role == WorkspaceRole.MANAGE

    @pytest.mark.django_db(transaction=True)
    def test_share_time_refresh_grants_access_then_adds(
        self, client, user, workspace, tenant, mocker
    ):
        """Target was granted access upstream after last login: the share-time
        refresh picks it up and the add succeeds without them reconnecting."""
        User.objects.create_user(email="late@example.com", password="pass")

        async def fake_refresh(target, providers):
            # simulate the resolver discovering the newly-granted access
            await TenantMembership.objects.acreate(user=target, tenant=tenant)
            return True

        mocker.patch(
            "apps.workspaces.api.workspace_views._arefresh_target_for_workspace", new=fake_refresh
        )
        client.force_login(user)
        resp = client.post(
            f"/api/workspaces/{workspace.id}/members/",
            {"email": "late@example.com", "role": WorkspaceRole.READ},
            content_type="application/json",
        )
        assert resp.status_code == 201

    def test_share_time_refresh_had_token_but_no_upstream_access(
        self, client, user, workspace, mocker, db
    ):
        """Target had a token (refresh ran) but still lacks upstream access → an
        awaiting_access invite, not a hard error."""
        User.objects.create_user(email="noaccess@example.com", password="pass")

        async def fake_refresh(target, providers):
            return True  # a token existed, but no new membership resulted

        mocker.patch(
            "apps.workspaces.api.workspace_views._arefresh_target_for_workspace", new=fake_refresh
        )
        client.force_login(user)
        resp = client.post(
            f"/api/workspaces/{workspace.id}/members/",
            {"email": "noaccess@example.com", "role": WorkspaceRole.READ},
            content_type="application/json",
        )
        assert resp.status_code == 201, resp.json()
        assert resp.json()["result"] == "invite_awaiting_access"

    def test_non_manager_cannot_add_members(self, client, workspace, tenant, db):
        writer = User.objects.create_user(email="wr@example.com", password="pass")
        TenantMembership.objects.create(user=writer, tenant=tenant)
        WorkspaceMembership.objects.create(
            workspace=workspace, user=writer, role=WorkspaceRole.READ_WRITE
        )
        target = User.objects.create_user(email="alice@example.com", password="pass")
        TenantMembership.objects.create(user=target, tenant=tenant)

        client.force_login(writer)
        resp = client.post(
            f"/api/workspaces/{workspace.id}/members/",
            {"email": "alice@example.com", "role": WorkspaceRole.READ},
            content_type="application/json",
        )
        assert resp.status_code == 403
        assert resp.json()["error"] == "Only managers can add members."

    def test_existing_member_returns_409(self, client, user, workspace, tenant, db):
        target = User.objects.create_user(email="alice@example.com", password="pass")
        TenantMembership.objects.create(user=target, tenant=tenant)
        WorkspaceMembership.objects.create(
            workspace=workspace, user=target, role=WorkspaceRole.READ
        )

        client.force_login(user)
        resp = client.post(
            f"/api/workspaces/{workspace.id}/members/",
            {"email": "alice@example.com", "role": WorkspaceRole.READ_WRITE},
            content_type="application/json",
        )
        assert resp.status_code == 409
        assert resp.json()["error"] == "User is already a member."

    def test_case_insensitive_duplicate_returns_409(self, client, user, workspace, tenant, db):
        """Adding ALICE@X.COM when alice@x.com is already a member should 409."""
        target = User.objects.create_user(email="alice@example.com", password="pass")
        TenantMembership.objects.create(user=target, tenant=tenant)
        WorkspaceMembership.objects.create(
            workspace=workspace, user=target, role=WorkspaceRole.READ
        )

        client.force_login(user)
        resp = client.post(
            f"/api/workspaces/{workspace.id}/members/",
            {"email": "ALICE@EXAMPLE.COM", "role": WorkspaceRole.READ_WRITE},
            content_type="application/json",
        )
        assert resp.status_code == 409
        assert resp.json()["error"] == "User is already a member."

    def test_add_with_role_read(self, client, user, workspace, tenant, db):
        target = User.objects.create_user(email="r@example.com", password="pass")
        TenantMembership.objects.create(user=target, tenant=tenant)

        client.force_login(user)
        resp = client.post(
            f"/api/workspaces/{workspace.id}/members/",
            {"email": "r@example.com", "role": WorkspaceRole.READ},
            content_type="application/json",
        )
        assert resp.status_code == 201
        assert resp.json()["role"] == WorkspaceRole.READ

    def test_add_with_role_manage(self, client, user, workspace, tenant, db):
        target = User.objects.create_user(email="m@example.com", password="pass")
        TenantMembership.objects.create(user=target, tenant=tenant)

        client.force_login(user)
        resp = client.post(
            f"/api/workspaces/{workspace.id}/members/",
            {"email": "m@example.com", "role": WorkspaceRole.MANAGE},
            content_type="application/json",
        )
        assert resp.status_code == 201
        assert resp.json()["role"] == WorkspaceRole.MANAGE


# ---------------------------------------------------------------------------
# Member list: members + live invites
# ---------------------------------------------------------------------------


class TestMemberListWithInvites:
    def test_get_members_returns_members_and_live_invites(
        self, client, user, workspace, tenant, db
    ):
        member = User.objects.create_user(email="member@example.com", password="pass")
        TenantMembership.objects.create(user=member, tenant=tenant)
        WorkspaceMembership.objects.create(
            workspace=workspace, user=member, role=WorkspaceRole.READ
        )
        WorkspaceInvite.objects.create(
            workspace=workspace,
            email="pending@example.com",
            role=WorkspaceRole.READ,
            status=WorkspaceInviteStatus.PENDING,
        )
        # A revoked invite must NOT appear.
        WorkspaceInvite.objects.create(
            workspace=workspace,
            email="revoked@example.com",
            role=WorkspaceRole.READ,
            status=WorkspaceInviteStatus.REVOKED,
        )

        client.force_login(user)
        resp = client.get(f"/api/workspaces/{workspace.id}/members/")
        assert resp.status_code == 200
        body = resp.json()
        member_emails = {m["email"] for m in body["members"]}
        assert {"test@example.com", "member@example.com"} <= member_emails
        invite_emails = {i["email"] for i in body["invites"]}
        assert invite_emails == {"pending@example.com"}
        assert body["invites"][0]["status"] == WorkspaceInviteStatus.PENDING


# ---------------------------------------------------------------------------
# Invite detail: revoke + role change
# ---------------------------------------------------------------------------


class TestInviteDetail:
    def _make_invite(self, workspace, email="pending@example.com", role=WorkspaceRole.READ):
        return WorkspaceInvite.objects.create(
            workspace=workspace,
            email=email,
            role=role,
            status=WorkspaceInviteStatus.PENDING,
        )

    def test_manager_can_revoke_invite(self, client, user, workspace):
        invite = self._make_invite(workspace)
        client.force_login(user)
        resp = client.delete(f"/api/workspaces/{workspace.id}/invites/{invite.id}/")
        assert resp.status_code == 204
        invite.refresh_from_db()
        assert invite.status == WorkspaceInviteStatus.REVOKED

    def test_manager_can_change_invite_role(self, client, user, workspace):
        invite = self._make_invite(workspace)
        client.force_login(user)
        resp = client.patch(
            f"/api/workspaces/{workspace.id}/invites/{invite.id}/",
            {"role": WorkspaceRole.MANAGE},
            content_type="application/json",
        )
        assert resp.status_code == 200
        invite.refresh_from_db()
        assert invite.role == WorkspaceRole.MANAGE

    def test_invalid_role_returns_400(self, client, user, workspace):
        invite = self._make_invite(workspace)
        client.force_login(user)
        resp = client.patch(
            f"/api/workspaces/{workspace.id}/invites/{invite.id}/",
            {"role": "admin"},
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_non_manager_cannot_revoke(self, client, workspace, tenant, db):
        reader = User.objects.create_user(email="reader2@example.com", password="pass")
        TenantMembership.objects.create(user=reader, tenant=tenant)
        WorkspaceMembership.objects.create(
            workspace=workspace, user=reader, role=WorkspaceRole.READ
        )
        invite = self._make_invite(workspace)
        client.force_login(reader)
        resp = client.delete(f"/api/workspaces/{workspace.id}/invites/{invite.id}/")
        assert resp.status_code == 403
        invite.refresh_from_db()
        assert invite.status == WorkspaceInviteStatus.PENDING

    def test_revoke_unknown_invite_returns_404(self, client, user, workspace):
        import uuid

        client.force_login(user)
        resp = client.delete(f"/api/workspaces/{workspace.id}/invites/{uuid.uuid4()}/")
        assert resp.status_code == 404

    def test_cannot_touch_invite_of_other_workspace(self, client, user, workspace, db):
        other_ws = Workspace.objects.create(name="Other")
        invite = self._make_invite(other_ws)
        client.force_login(user)
        resp = client.delete(f"/api/workspaces/{workspace.id}/invites/{invite.id}/")
        assert resp.status_code == 404
