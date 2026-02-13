"""Tests for core models."""
import pytest
from django.contrib.auth import get_user_model
from django.db import IntegrityError

from apps.projects.models import DatabaseConnection
from apps.projects.models import (
    Project,
    ProjectMembership,
    ProjectRole,
)
from apps.knowledge.models import (
    AgentLearning,
    GoldenQuery,
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


class TestProjectModel:
    """Tests for the Project model."""

    def test_create_project(self, db_connection, user):
        project = Project.objects.create(
            name="Test Project",
            slug="test-project",
            database_connection=db_connection,
            created_by=user,
        )

        assert project.name == "Test Project"
        assert project.slug == "test-project"
        assert str(project) == "Test Project"

    def test_get_connection_params(self, db, user):
        conn = DatabaseConnection(
            name="Params Test Connection",
            db_host="dbhost.example.com",
            db_port=5433,
            db_name="mydb",
            created_by=user,
        )
        conn.db_user = "dbuser"
        conn.db_password = "dbpass"
        conn.save()

        project = Project.objects.create(
            name="Connection Test",
            slug="conn-test",
            database_connection=conn,
            db_schema="analytics",
            max_query_timeout_seconds=60,
            created_by=user,
        )

        params = project.get_connection_params()

        assert params["host"] == "dbhost.example.com"
        assert params["port"] == 5433
        assert params["dbname"] == "mydb"
        assert params["user"] == "dbuser"
        assert params["password"] == "dbpass"
        assert "search_path=analytics,public" in params["options"]
        assert "statement_timeout=60000" in params["options"]


class TestProjectMembership:
    """Tests for ProjectMembership model."""

    def test_create_membership(self, db_connection, user):
        project = Project.objects.create(
            name="Team Project",
            slug="team-project",
            database_connection=db_connection,
            created_by=user,
        )

        membership = ProjectMembership.objects.create(
            user=user,
            project=project,
            role=ProjectRole.ANALYST,
        )

        assert membership.role == ProjectRole.ANALYST
        assert str(membership) == f"{user} - {project} (analyst)"

    def test_unique_membership(self, db_connection, user):
        project = Project.objects.create(
            name="Unique Test",
            slug="unique-test",
            database_connection=db_connection,
            created_by=user,
        )

        ProjectMembership.objects.create(user=user, project=project)

        with pytest.raises(IntegrityError):
            ProjectMembership.objects.create(user=user, project=project)


class TestKnowledgeModels:
    """Tests for Knowledge layer models."""

    @pytest.fixture
    def project(self, db_connection, user):
        return Project.objects.create(
            name="Knowledge Test",
            slug="knowledge-test",
            database_connection=db_connection,
            created_by=user,
        )

    def test_table_knowledge(self, project, user):
        tk = TableKnowledge.objects.create(
            project=project,
            table_name="orders",
            description="Customer orders table",
            use_cases=["Revenue reporting", "Order analysis"],
            data_quality_notes=["created_at is UTC"],
            column_notes={"status": "Values: pending, completed, cancelled"},
            updated_by=user,
        )

        assert tk.table_name == "orders"
        assert "Revenue reporting" in tk.use_cases
        assert str(tk) == "orders (Knowledge Test)"

    def test_knowledge_entry(self, project, user):
        entry = KnowledgeEntry.objects.create(
            project=project,
            title="MRR",
            content="Monthly Recurring Revenue from active subscriptions\n\n```sql\nSELECT SUM(amount) FROM subscriptions WHERE status = 'active'\n```",
            tags=["metric", "finance"],
            created_by=user,
        )

        assert entry.title == "MRR"
        assert "metric" in entry.tags
        assert str(entry) == "MRR (Knowledge Test)"

    def test_agent_learning(self, project, user):
        learning = AgentLearning.objects.create(
            project=project,
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

    def test_golden_query(self, project, user):
        gq = GoldenQuery.objects.create(
            project=project,
            question="What is the total revenue for January 2024?",
            expected_sql="SELECT SUM(amount) FROM orders WHERE date >= '2024-01-01' AND date < '2024-02-01'",
            expected_result={"total": 50000},
            comparison_mode="exact",
            difficulty="easy",
            created_by=user,
        )

        assert gq.difficulty == "easy"
        assert gq.comparison_mode == "exact"
