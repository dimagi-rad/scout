"""Tests for CustomWorkspace, CustomWorkspaceTenant, and WorkspaceMembership models."""

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError

from apps.users.models import TenantMembership
from apps.workspace.models import (
    CustomWorkspace,
    CustomWorkspaceTenant,
    TenantWorkspace,
    WorkspaceMembership,
)

User = get_user_model()


@pytest.fixture
def owner(db):
    return User.objects.create_user(
        email="owner@example.com",
        password="ownerpass123",
        first_name="Workspace",
        last_name="Owner",
    )


@pytest.fixture
def member(db):
    return User.objects.create_user(
        email="member@example.com",
        password="memberpass123",
        first_name="Workspace",
        last_name="Member",
    )


@pytest.fixture
def tenant_membership_a(db, owner):
    return TenantMembership.objects.create(
        user=owner,
        provider="commcare",
        tenant_id="domain-a",
        tenant_name="Domain A",
    )


@pytest.fixture
def tenant_membership_b(db, owner):
    return TenantMembership.objects.create(
        user=owner,
        provider="commcare",
        tenant_id="domain-b",
        tenant_name="Domain B",
    )


@pytest.fixture
def tenant_workspace_a(db):
    return TenantWorkspace.objects.create(
        tenant_id="domain-a",
        tenant_name="Domain A",
    )


@pytest.fixture
def tenant_workspace_b(db):
    return TenantWorkspace.objects.create(
        tenant_id="domain-b",
        tenant_name="Domain B",
    )


@pytest.mark.django_db
class TestCustomWorkspaceModel:
    def test_create_custom_workspace(self, owner):
        ws = CustomWorkspace.objects.create(
            name="My Custom Workspace",
            description="A workspace spanning multiple tenants",
            created_by=owner,
        )
        assert ws.name == "My Custom Workspace"
        assert ws.description == "A workspace spanning multiple tenants"
        assert ws.created_by == owner
        assert ws.system_prompt == ""
        assert ws.id is not None
        assert ws.created_at is not None
        assert ws.updated_at is not None
        assert str(ws) == "My Custom Workspace"

    def test_add_tenants_to_workspace(self, owner, tenant_workspace_a, tenant_workspace_b):
        ws = CustomWorkspace.objects.create(name="Multi-Tenant WS", created_by=owner)
        CustomWorkspaceTenant.objects.create(workspace=ws, tenant_workspace=tenant_workspace_a)
        CustomWorkspaceTenant.objects.create(workspace=ws, tenant_workspace=tenant_workspace_b)
        assert ws.custom_workspace_tenants.count() == 2

    def test_duplicate_tenant_rejected(self, owner, tenant_workspace_a):
        ws = CustomWorkspace.objects.create(name="Dup Test WS", created_by=owner)
        CustomWorkspaceTenant.objects.create(workspace=ws, tenant_workspace=tenant_workspace_a)
        with pytest.raises(IntegrityError):
            CustomWorkspaceTenant.objects.create(workspace=ws, tenant_workspace=tenant_workspace_a)


@pytest.mark.django_db
class TestWorkspaceMembership:
    def test_create_membership(self, owner):
        ws = CustomWorkspace.objects.create(name="Membership WS", created_by=owner)
        membership = WorkspaceMembership.objects.create(
            workspace=ws,
            user=owner,
            role="owner",
            invited_by=None,
        )
        assert membership.workspace == ws
        assert membership.user == owner
        assert membership.role == "owner"
        assert membership.invited_by is None
        assert membership.joined_at is not None
        assert str(membership) == f"{owner.email} - owner in Membership WS"

    def test_duplicate_membership_rejected(self, owner):
        ws = CustomWorkspace.objects.create(name="Dup Membership WS", created_by=owner)
        WorkspaceMembership.objects.create(workspace=ws, user=owner, role="owner")
        with pytest.raises(IntegrityError):
            WorkspaceMembership.objects.create(workspace=ws, user=owner, role="editor")

    def test_role_choices(self, owner, member):
        ws = CustomWorkspace.objects.create(name="Roles WS", created_by=owner)
        for role in ("owner", "editor", "viewer"):
            # Use a fresh user for each role to avoid unique_together conflict
            if role == "owner":
                u = owner
            elif role == "editor":
                u = member
            else:
                u = User.objects.create_user(email="viewer@example.com", password="viewerpass123")
            m = WorkspaceMembership.objects.create(workspace=ws, user=u, role=role)
            assert m.role == role


@pytest.mark.django_db
class TestKnowledgeDualFK:
    def test_knowledge_entry_on_tenant_workspace(self, tenant_workspace_a):
        from apps.knowledge.models import KnowledgeEntry

        entry = KnowledgeEntry.objects.create(
            workspace=tenant_workspace_a,
            title="Tenant Knowledge",
            content="Some content",
        )
        assert entry.workspace == tenant_workspace_a
        assert entry.custom_workspace is None

    def test_knowledge_entry_on_custom_workspace(self, owner):
        from apps.knowledge.models import KnowledgeEntry

        ws = CustomWorkspace.objects.create(name="Test", created_by=owner)
        entry = KnowledgeEntry.objects.create(
            custom_workspace=ws,
            title="Custom Knowledge",
            content="Some content",
        )
        assert entry.custom_workspace == ws
        assert entry.workspace is None

    def test_agent_learning_on_custom_workspace(self, owner):
        from apps.knowledge.models import AgentLearning

        ws = CustomWorkspace.objects.create(name="Test", created_by=owner)
        learning = AgentLearning.objects.create(
            custom_workspace=ws,
            description="Test learning",
        )
        assert learning.custom_workspace == ws
        assert learning.workspace is None


@pytest.mark.django_db
class TestThreadCustomWorkspaceFK:
    def test_thread_with_custom_workspace(self, owner):
        from apps.chat.models import Thread

        ws = CustomWorkspace.objects.create(name="Thread WS", created_by=owner)
        thread = Thread.objects.create(
            custom_workspace=ws,
            user=owner,
            title="Test thread",
        )
        assert thread.custom_workspace == ws
        assert thread.tenant_membership is None


@pytest.mark.django_db
class TestCleanValidation:
    """Test clean() methods enforce XOR constraint at the application layer."""

    def test_knowledge_entry_clean_no_workspace_raises(self):
        from apps.knowledge.models import KnowledgeEntry

        entry = KnowledgeEntry(title="Orphan", content="No workspace")
        with pytest.raises(ValidationError):
            entry.clean()

    def test_knowledge_entry_clean_both_workspaces_raises(self, owner, tenant_workspace_a):
        from apps.knowledge.models import KnowledgeEntry

        ws = CustomWorkspace.objects.create(name="Test", created_by=owner)
        entry = KnowledgeEntry(
            workspace=tenant_workspace_a, custom_workspace=ws, title="Both", content="Bad"
        )
        with pytest.raises(ValidationError):
            entry.clean()

    def test_table_knowledge_clean_no_workspace_raises(self):
        from apps.knowledge.models import TableKnowledge

        tk = TableKnowledge(table_name="orphan", description="No workspace")
        with pytest.raises(ValidationError):
            tk.clean()

    def test_agent_learning_clean_no_workspace_raises(self):
        from apps.knowledge.models import AgentLearning

        al = AgentLearning(description="Orphan learning")
        with pytest.raises(ValidationError):
            al.clean()

    def test_thread_clean_no_context_raises(self, owner):
        from apps.chat.models import Thread

        thread = Thread(user=owner, title="Orphan thread")
        with pytest.raises(ValidationError):
            thread.clean()

    def test_thread_clean_both_contexts_raises(self, owner, tenant_membership_a):
        from apps.chat.models import Thread

        ws = CustomWorkspace.objects.create(name="Test", created_by=owner)
        thread = Thread(
            user=owner, tenant_membership=tenant_membership_a, custom_workspace=ws, title="Both"
        )
        with pytest.raises(ValidationError):
            thread.clean()


@pytest.mark.django_db
class TestWorkspaceContextQuerySet:
    """Test the shared WorkspaceContextQuerySet.for_workspace_context() manager."""

    def test_filter_by_tenant_workspace(self, tenant_workspace_a, owner):
        from apps.knowledge.models import KnowledgeEntry

        KnowledgeEntry.objects.create(
            workspace=tenant_workspace_a, title="Tenant entry", content="x"
        )
        qs = KnowledgeEntry.objects.for_workspace_context(tenant_workspace_a)
        assert qs.count() == 1
        assert qs.first().title == "Tenant entry"

    def test_filter_by_custom_workspace(self, owner):
        from apps.knowledge.models import KnowledgeEntry

        ws = CustomWorkspace.objects.create(name="CW", created_by=owner)
        KnowledgeEntry.objects.create(custom_workspace=ws, title="Custom entry", content="x")
        qs = KnowledgeEntry.objects.for_workspace_context(ws)
        assert qs.count() == 1
        assert qs.first().title == "Custom entry"

    def test_filter_excludes_other_workspace(self, tenant_workspace_a, tenant_workspace_b):
        from apps.knowledge.models import KnowledgeEntry

        KnowledgeEntry.objects.create(workspace=tenant_workspace_a, title="A entry", content="x")
        KnowledgeEntry.objects.create(workspace=tenant_workspace_b, title="B entry", content="x")
        qs = KnowledgeEntry.objects.for_workspace_context(tenant_workspace_a)
        assert qs.count() == 1
        assert qs.first().title == "A entry"
