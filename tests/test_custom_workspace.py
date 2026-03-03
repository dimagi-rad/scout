"""Tests for CustomWorkspace, CustomWorkspaceTenant, and WorkspaceMembership models."""

import pytest
from django.contrib.auth import get_user_model
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
class TestCustomWorkspaceContext:
    def test_build_context_aggregates_knowledge(
        self, owner, tenant_workspace_a, tenant_workspace_b
    ):
        from apps.agents.graph.base import _build_custom_workspace_context
        from apps.knowledge.models import KnowledgeEntry

        ws = CustomWorkspace.objects.create(name="Context Test", created_by=owner)
        CustomWorkspaceTenant.objects.create(workspace=ws, tenant_workspace=tenant_workspace_a)
        CustomWorkspaceTenant.objects.create(workspace=ws, tenant_workspace=tenant_workspace_b)

        # Create knowledge on each tenant workspace
        KnowledgeEntry.objects.create(
            workspace=tenant_workspace_a, title="Tenant A Knowledge", content="Content A"
        )
        KnowledgeEntry.objects.create(
            workspace=tenant_workspace_b, title="Tenant B Knowledge", content="Content B"
        )
        # Create workspace-specific knowledge
        KnowledgeEntry.objects.create(
            custom_workspace=ws, title="Workspace Knowledge", content="Content WS"
        )

        context = _build_custom_workspace_context(ws)
        assert len(context["knowledge"]) == 3
        assert len(context["available_tenants"]) == 2

    def test_build_context_aggregates_system_prompts(self, owner, tenant_workspace_a):
        from apps.agents.graph.base import _build_custom_workspace_context

        tenant_workspace_a.system_prompt = "Tenant prompt"
        tenant_workspace_a.save()

        ws = CustomWorkspace.objects.create(
            name="Prompt Test", created_by=owner, system_prompt="Workspace prompt"
        )
        CustomWorkspaceTenant.objects.create(workspace=ws, tenant_workspace=tenant_workspace_a)

        context = _build_custom_workspace_context(ws)
        assert len(context["system_prompts"]) == 2
        assert "Workspace prompt" in context["system_prompts"][0]
        assert "Tenant prompt" in context["system_prompts"][1]

    def test_build_context_aggregates_learnings(self, owner, tenant_workspace_a):
        from apps.agents.graph.base import _build_custom_workspace_context
        from apps.knowledge.models import AgentLearning

        ws = CustomWorkspace.objects.create(name="Learning Test", created_by=owner)
        CustomWorkspaceTenant.objects.create(workspace=ws, tenant_workspace=tenant_workspace_a)

        # Create learnings on tenant and workspace
        AgentLearning.objects.create(
            workspace=tenant_workspace_a,
            description="Tenant learning",
            is_active=True,
            confidence_score=0.9,
        )
        AgentLearning.objects.create(
            custom_workspace=ws,
            description="Workspace learning",
            is_active=True,
            confidence_score=0.8,
        )
        # Inactive learning should be excluded
        AgentLearning.objects.create(
            workspace=tenant_workspace_a,
            description="Inactive learning",
            is_active=False,
            confidence_score=0.7,
        )

        context = _build_custom_workspace_context(ws)
        assert len(context["learnings"]) == 2
        # Should be ordered by confidence_score descending
        assert context["learnings"][0].confidence_score == 0.9
        assert context["learnings"][1].confidence_score == 0.8

    def test_build_context_empty_workspace(self, owner):
        from apps.agents.graph.base import _build_custom_workspace_context

        ws = CustomWorkspace.objects.create(name="Empty WS", created_by=owner)

        context = _build_custom_workspace_context(ws)
        assert context["system_prompts"] == []
        assert context["knowledge"] == []
        assert context["learnings"] == []
        assert context["available_tenants"] == []

    def test_build_context_tenant_data_dictionary_flag(self, owner, tenant_workspace_a):
        from apps.agents.graph.base import _build_custom_workspace_context

        ws = CustomWorkspace.objects.create(name="Dict Test", created_by=owner)
        CustomWorkspaceTenant.objects.create(workspace=ws, tenant_workspace=tenant_workspace_a)

        context = _build_custom_workspace_context(ws)
        assert len(context["available_tenants"]) == 1
        assert context["available_tenants"][0]["has_data_dictionary"] is False

        # Set data dictionary and verify
        tenant_workspace_a.data_dictionary = {"tables": ["users"]}
        tenant_workspace_a.save()

        context = _build_custom_workspace_context(ws)
        assert context["available_tenants"][0]["has_data_dictionary"] is True
