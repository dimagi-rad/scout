"""Tests for core models."""

import pytest
from django.contrib.auth import get_user_model

from apps.knowledge.models import (
    AgentLearning,
    KnowledgeEntry,
    TableKnowledge,
)

User = get_user_model()


class TestUserModel:
    """Tests for the custom User model."""

    def test_create_user(self, db):
        user = User.objects.create_user(
            email="newuser@example.com",
            password="testpass123",
        )
        assert user.email == "newuser@example.com"
        assert user.check_password("testpass123")
        assert not user.is_staff
        assert not user.is_superuser

    def test_create_superuser(self, db):
        admin = User.objects.create_superuser(
            email="admin@example.com",
            password="adminpass123",
        )
        assert admin.is_staff
        assert admin.is_superuser

    def test_get_full_name(self, user):
        assert user.get_full_name() == "Test User"

    def test_get_full_name_empty(self, db):
        user = User.objects.create_user(email="noname@example.com", password="test")
        assert user.get_full_name() == "noname@example.com"


class TestKnowledgeModels:
    """Tests for Knowledge layer models."""

    def test_table_knowledge(self, workspace, user):
        tk = TableKnowledge.objects.create(
            workspace=workspace,
            table_name="orders",
            description="Customer orders table",
            use_cases=["Revenue reporting", "Order analysis"],
            data_quality_notes=["created_at is UTC"],
            column_notes={"status": "Values: pending, completed, cancelled"},
            updated_by=user,
        )

        assert tk.table_name == "orders"
        assert "Revenue reporting" in tk.use_cases
        assert str(tk) == f"orders ({workspace.tenant_name})"

    def test_knowledge_entry(self, workspace, user):
        entry = KnowledgeEntry.objects.create(
            workspace=workspace,
            title="MRR",
            content="Monthly Recurring Revenue from active subscriptions\n\n```sql\nSELECT SUM(amount) FROM subscriptions WHERE status = 'active'\n```",
            tags=["metric", "finance"],
            created_by=user,
        )

        assert entry.title == "MRR"
        assert "metric" in entry.tags
        assert str(entry) == f"MRR ({workspace.tenant_name})"

    def test_agent_learning(self, workspace, user):
        learning = AgentLearning.objects.create(
            workspace=workspace,
            description="Amount column is in cents, not dollars. Divide by 100.",
            category="type_mismatch",
            applies_to_tables=["orders"],
            original_error="Unexpected revenue value",
            original_sql="SELECT amount FROM orders",
            corrected_sql="SELECT amount / 100.0 FROM orders",
            discovered_by_user=user,
        )

        assert learning.category == "type_mismatch"
        assert learning.is_active
        assert learning.confidence_score == 0.5


@pytest.mark.django_db
class TestTenantMetadata:
    def test_create_and_retrieve_metadata(self, tenant_membership):
        from django.utils import timezone

        from apps.projects.models import TenantMetadata

        payload = {
            "case_types": ["patient", "household"],
            "app_definitions": [{"id": "abc", "name": "CHW App"}],
        }
        meta = TenantMetadata.objects.create(
            tenant_membership=tenant_membership,
            metadata=payload,
            discovered_at=timezone.now(),
        )
        retrieved = TenantMetadata.objects.get(pk=meta.pk)
        assert retrieved.metadata["case_types"] == ["patient", "household"]
        assert retrieved.metadata["app_definitions"][0]["id"] == "abc"

    def test_one_to_one_with_tenant_membership(self, tenant_membership):
        from apps.projects.models import TenantMetadata

        TenantMetadata.objects.create(tenant_membership=tenant_membership)
        with pytest.raises(Exception, match="unique constraint"):  # noqa: B017
            TenantMetadata.objects.create(tenant_membership=tenant_membership)

    def test_metadata_defaults_to_empty_dict(self, tenant_membership):
        from apps.projects.models import TenantMetadata

        meta = TenantMetadata.objects.create(tenant_membership=tenant_membership)
        assert meta.metadata == {}
