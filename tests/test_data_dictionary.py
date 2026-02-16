"""
Tests for the DataDictionaryGenerator service.

Note: These tests require a PostgreSQL database connection.
For CI, consider using pytest-postgresql or a test database.
"""
import pytest
from unittest.mock import MagicMock, patch

from apps.projects.services.data_dictionary import DataDictionaryGenerator


class TestDataDictionaryGenerator:
    """Tests for DataDictionaryGenerator."""

    @pytest.fixture
    def mock_project(self, db_connection, user):
        """Create a mock project for testing."""
        from apps.projects.models import Project

        return Project.objects.create(
            name="Test Project",
            slug="test-project",
            database_connection=db_connection,
            db_schema="public",
            allowed_tables=[],
            excluded_tables=[],
            created_by=user,
        )

    def test_get_visible_tables_all(self, mock_project):
        """Test that all tables are visible when no filters set."""
        generator = DataDictionaryGenerator(mock_project)
        all_tables = ["users", "orders", "products"]
        visible = generator._get_visible_tables(all_tables)
        assert visible == ["users", "orders", "products"]

    def test_get_visible_tables_allowed(self, mock_project):
        """Test filtering by allowed_tables."""
        mock_project.allowed_tables = ["users", "orders"]
        mock_project.save()

        generator = DataDictionaryGenerator(mock_project)
        all_tables = ["users", "orders", "products", "secret_table"]
        visible = generator._get_visible_tables(all_tables)

        assert set(visible) == {"users", "orders"}

    def test_get_visible_tables_excluded(self, mock_project):
        """Test filtering by excluded_tables."""
        mock_project.excluded_tables = ["secret_table", "internal_logs"]
        mock_project.save()

        generator = DataDictionaryGenerator(mock_project)
        all_tables = ["users", "orders", "secret_table", "internal_logs"]
        visible = generator._get_visible_tables(all_tables)

        assert set(visible) == {"users", "orders"}

    def test_get_visible_tables_both_filters(self, mock_project):
        """Test filtering by both allowed and excluded tables."""
        mock_project.allowed_tables = ["users", "orders", "products"]
        mock_project.excluded_tables = ["products"]
        mock_project.save()

        generator = DataDictionaryGenerator(mock_project)
        all_tables = ["users", "orders", "products", "other"]
        visible = generator._get_visible_tables(all_tables)

        assert set(visible) == {"users", "orders"}

    def test_is_sensitive_column(self, mock_project):
        """Test sensitive column detection."""
        generator = DataDictionaryGenerator(mock_project)

        # Sensitive columns
        assert generator._is_sensitive_column("password")
        assert generator._is_sensitive_column("user_password")
        assert generator._is_sensitive_column("password_hash")
        assert generator._is_sensitive_column("api_key")
        assert generator._is_sensitive_column("auth_token")
        assert generator._is_sensitive_column("ssn")
        assert generator._is_sensitive_column("credit_card_number")

        # Non-sensitive columns
        assert not generator._is_sensitive_column("username")
        assert not generator._is_sensitive_column("email")
        assert not generator._is_sensitive_column("created_at")
        assert not generator._is_sensitive_column("amount")

    def test_render_for_prompt_no_dictionary(self, mock_project):
        """Test render_for_prompt when no dictionary exists."""
        generator = DataDictionaryGenerator(mock_project)
        result = generator.render_for_prompt()
        assert "No data dictionary available" in result

    def test_render_for_prompt_small_schema(self, mock_project):
        """Test render_for_prompt with a small schema (inline detail)."""
        mock_project.data_dictionary = {
            "schema": "public",
            "generated_at": "2024-01-01T00:00:00",
            "tables": {
                "users": {
                    "comment": "User accounts",
                    "row_count": 1000,
                    "columns": [
                        {
                            "name": "id",
                            "type": "integer",
                            "nullable": False,
                            "default": None,
                            "comment": "Primary key",
                            "is_primary_key": True,
                            "sample_values": ["1", "2", "3"],
                        },
                        {
                            "name": "email",
                            "type": "varchar(255)",
                            "nullable": False,
                            "default": None,
                            "comment": "User email",
                            "is_primary_key": False,
                            "sample_values": ["a@b.com"],
                        },
                    ],
                    "primary_key": ["id"],
                    "foreign_keys": [],
                    "indexes": [],
                },
            },
            "enums": {"status": ["active", "inactive"]},
        }
        mock_project.save()

        generator = DataDictionaryGenerator(mock_project)
        result = generator.render_for_prompt()

        assert "## Database Schema: public" in result
        assert "### users" in result
        assert "User accounts" in result
        assert "| id | integer |" in result
        assert "status" in result  # Enum
        assert "active, inactive" in result

    def test_render_for_prompt_large_schema(self, mock_project):
        """Test render_for_prompt with a large schema (listing only)."""
        # Create a schema with more than 15 tables
        tables = {}
        for i in range(20):
            tables[f"table_{i}"] = {
                "comment": f"Table {i} description",
                "row_count": i * 100,
                "columns": [{"name": "id", "type": "integer", "nullable": False, "default": None, "comment": "", "is_primary_key": True, "sample_values": []}],
                "primary_key": ["id"],
                "foreign_keys": [],
                "indexes": [],
            }

        mock_project.data_dictionary = {
            "schema": "public",
            "generated_at": "2024-01-01T00:00:00",
            "tables": tables,
            "enums": {},
        }
        mock_project.save()

        generator = DataDictionaryGenerator(mock_project)
        result = generator.render_for_prompt()

        # Should use listing format, not inline detail
        assert "use `describe_table` tool" in result
        assert "- **table_0**" in result
        # Should NOT have column details inline
        assert "| Column | Type |" not in result


class TestDataDictionaryGeneratorIntegration:
    """Integration tests requiring a real PostgreSQL connection.

    These tests are skipped by default. To run them:
    1. Set up a test PostgreSQL database
    2. Set TEST_DATABASE_URL environment variable
    3. Run: pytest tests/test_data_dictionary.py::TestDataDictionaryGeneratorIntegration -v
    """

    @pytest.fixture
    def real_project(self, db, user):
        """Create a project pointing to a real test database."""
        import os
        from apps.projects.models import DatabaseConnection
        from apps.projects.models import Project

        db_url = os.environ.get("TEST_DATABASE_URL")
        if not db_url:
            pytest.skip("TEST_DATABASE_URL not set")

        # Parse the URL (format: postgresql://user:pass@host:port/dbname)
        from urllib.parse import urlparse
        parsed = urlparse(db_url)

        conn = DatabaseConnection(
            name="Integration Test Connection",
            db_host=parsed.hostname,
            db_port=parsed.port or 5432,
            db_name=parsed.path[1:],
            created_by=user,
        )
        conn.db_user = parsed.username
        conn.db_password = parsed.password
        conn.save()

        return Project.objects.create(
            name="Integration Test",
            slug="integration-test",
            database_connection=conn,
            db_schema="public",
            created_by=user,
        )

    @pytest.mark.skip(reason="Requires TEST_DATABASE_URL")
    def test_generate_real_database(self, real_project):
        """Test generating a data dictionary from a real database."""
        generator = DataDictionaryGenerator(real_project)
        dictionary = generator.generate()

        assert "schema" in dictionary
        assert "tables" in dictionary
        assert "generated_at" in dictionary
        assert real_project.data_dictionary is not None
