"""
Tests for core models.
"""
import pytest
from django.contrib.auth import get_user_model
from django.db import IntegrityError

from apps.projects.models import (
    ConversationLog,
    Project,
    ProjectMembership,
    ProjectRole,
    SavedQuery,
)
from apps.knowledge.models import (
    AgentLearning,
    BusinessRule,
    CanonicalMetric,
    GoldenQuery,
    TableKnowledge,
    VerifiedQuery,
)

User = get_user_model()


class TestUserModel:
    """Tests for the custom User model."""

    def test_create_user(self, db):
        """Test creating a user with email."""
        user = User.objects.create_user(
            email="newuser@example.com",
            password="testpass123",
        )
        assert user.email == "newuser@example.com"
        assert user.check_password("testpass123")
        assert not user.is_staff
        assert not user.is_superuser

    def test_create_superuser(self, db):
        """Test creating a superuser."""
        admin = User.objects.create_superuser(
            email="admin@example.com",
            password="adminpass123",
        )
        assert admin.is_staff
        assert admin.is_superuser

    def test_get_full_name(self, user):
        """Test get_full_name returns properly formatted name."""
        assert user.get_full_name() == "Test User"

    def test_get_full_name_empty(self, db):
        """Test get_full_name returns email when no name set."""
        user = User.objects.create_user(email="noname@example.com", password="test")
        assert user.get_full_name() == "noname@example.com"


class TestProjectModel:
    """Tests for the Project model."""

    def test_create_project(self, db, user):
        """Test creating a project."""
        project = Project.objects.create(
            name="Test Project",
            slug="test-project",
            db_host="localhost",
            db_name="testdb",
            created_by=user,
        )
        project.db_user = "testuser"
        project.db_password = "testpass"
        project.save()

        assert project.name == "Test Project"
        assert project.slug == "test-project"
        assert str(project) == "Test Project"

    def test_credential_encryption(self, db, user):
        """Test that credentials are encrypted and decrypted correctly."""
        project = Project.objects.create(
            name="Encrypted Project",
            slug="encrypted-project",
            db_host="localhost",
            db_name="testdb",
            created_by=user,
        )
        project.db_user = "secretuser"
        project.db_password = "secretpass123!"
        project.save()

        # Refresh from database to ensure we're testing stored values
        project.refresh_from_db()

        # Values should decrypt correctly
        assert project.db_user == "secretuser"
        assert project.db_password == "secretpass123!"

        # Raw values should be encrypted (binary)
        assert project._db_user != b"secretuser"
        assert project._db_password != b"secretpass123!"

        # Verify Fernet encryption format (base64-encoded, starts with 'gAAAAA')
        # Fernet tokens are base64-encoded and start with version byte 0x80 which encodes as 'gA'
        raw_user = project._db_user
        raw_password = project._db_password
        if isinstance(raw_user, bytes):
            raw_user_str = raw_user.decode("utf-8") if raw_user else ""
        else:
            raw_user_str = str(raw_user) if raw_user else ""
        if isinstance(raw_password, bytes):
            raw_password_str = raw_password.decode("utf-8") if raw_password else ""
        else:
            raw_password_str = str(raw_password) if raw_password else ""
        assert raw_user_str.startswith("gAAAAA"), f"Expected Fernet format, got: {raw_user_str[:20]}"
        assert raw_password_str.startswith("gAAAAA"), f"Expected Fernet format, got: {raw_password_str[:20]}"

    def test_get_connection_params(self, db, user):
        """Test get_connection_params returns correct dict."""
        project = Project.objects.create(
            name="Connection Test",
            slug="conn-test",
            db_host="dbhost.example.com",
            db_port=5433,
            db_name="mydb",
            db_schema="analytics",
            max_query_timeout_seconds=60,
            created_by=user,
        )
        project.db_user = "dbuser"
        project.db_password = "dbpass"
        project.save()

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

    def test_create_membership(self, db, user):
        """Test creating a project membership."""
        project = Project.objects.create(
            name="Team Project",
            slug="team-project",
            db_host="localhost",
            db_name="testdb",
            created_by=user,
        )
        project.db_user = "user"
        project.db_password = "pass"
        project.save()

        membership = ProjectMembership.objects.create(
            user=user,
            project=project,
            role=ProjectRole.ANALYST,
        )

        assert membership.role == ProjectRole.ANALYST
        assert str(membership) == f"{user} - {project} (analyst)"

    def test_unique_membership(self, db, user):
        """Test that user can only have one membership per project."""
        project = Project.objects.create(
            name="Unique Test",
            slug="unique-test",
            db_host="localhost",
            db_name="testdb",
            created_by=user,
        )
        project.db_user = "user"
        project.db_password = "pass"
        project.save()

        ProjectMembership.objects.create(user=user, project=project)

        with pytest.raises(IntegrityError):
            ProjectMembership.objects.create(user=user, project=project)


class TestKnowledgeModels:
    """Tests for Knowledge layer models."""

    @pytest.fixture
    def project(self, db, user):
        """Create a project for knowledge tests."""
        project = Project.objects.create(
            name="Knowledge Test",
            slug="knowledge-test",
            db_host="localhost",
            db_name="testdb",
            created_by=user,
        )
        project.db_user = "user"
        project.db_password = "pass"
        project.save()
        return project

    def test_table_knowledge(self, project, user):
        """Test TableKnowledge model."""
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

    def test_canonical_metric(self, project, user):
        """Test CanonicalMetric model."""
        metric = CanonicalMetric.objects.create(
            project=project,
            name="MRR",
            definition="Monthly Recurring Revenue from active subscriptions",
            sql_template="SELECT SUM(amount) FROM subscriptions WHERE status = 'active'",
            unit="USD",
            caveats=["Excludes annual contracts"],
            updated_by=user,
        )

        assert metric.name == "MRR"
        assert metric.unit == "USD"
        assert str(metric) == "MRR (Knowledge Test)"

    def test_verified_query(self, project, user):
        """Test VerifiedQuery model."""
        vq = VerifiedQuery.objects.create(
            project=project,
            name="Daily Revenue",
            description="Get daily revenue totals",
            sql="SELECT date, SUM(amount) FROM orders GROUP BY date",
            tables_used=["orders"],
            verified_by=user,
        )

        assert vq.name == "Daily Revenue"
        assert "orders" in vq.tables_used

    def test_business_rule(self, project, user):
        """Test BusinessRule model."""
        rule = BusinessRule.objects.create(
            project=project,
            title="Soft Delete Rule",
            description="Always filter deleted_at IS NULL for active records",
            applies_to_tables=["users", "orders"],
            created_by=user,
        )

        assert rule.title == "Soft Delete Rule"
        assert "users" in rule.applies_to_tables

    def test_agent_learning(self, project, user):
        """Test AgentLearning model."""
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
        """Test GoldenQuery model."""
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
