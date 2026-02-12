"""
Tests for the data dictionary API views.

Covers:
- DataDictionaryView (GET merged dictionary with annotations)
- TableAnnotationsView (GET/PUT individual table annotations)
- RefreshSchemaView (POST schema refresh — permission checks only, DB introspection mocked)
- Permission matrix: admin, analyst, viewer, non-member, unauthenticated
"""
import uuid

import pytest
from django.test import override_settings
from rest_framework.test import APIClient

from apps.knowledge.models import TableKnowledge
from apps.projects.models import Project, ProjectMembership, ProjectRole


# ---------------------------------------------------------------------------
# Sample data dictionary stored on a project (mimics RefreshSchemaView output)
# ---------------------------------------------------------------------------
SAMPLE_DATA_DICTIONARY = {
    "tables": {
        "public.users": {
            "schema": "public",
            "name": "users",
            "type": "BASE TABLE",
            "columns": [
                {
                    "name": "id",
                    "data_type": "integer",
                    "nullable": False,
                    "default": "nextval('users_id_seq'::regclass)",
                    "ordinal_position": 1,
                    "primary_key": True,
                },
                {
                    "name": "email",
                    "data_type": "character varying",
                    "nullable": False,
                    "default": None,
                    "ordinal_position": 2,
                    "max_length": 255,
                },
                {
                    "name": "created_at",
                    "data_type": "timestamp with time zone",
                    "nullable": False,
                    "default": "now()",
                    "ordinal_position": 3,
                },
            ],
            "primary_key": ["id"],
        },
        "public.orders": {
            "schema": "public",
            "name": "orders",
            "type": "BASE TABLE",
            "columns": [
                {
                    "name": "id",
                    "data_type": "integer",
                    "nullable": False,
                    "default": None,
                    "ordinal_position": 1,
                    "primary_key": True,
                },
                {
                    "name": "user_id",
                    "data_type": "integer",
                    "nullable": False,
                    "default": None,
                    "ordinal_position": 2,
                    "foreign_key": {
                        "references_schema": "public",
                        "references_table": "users",
                        "references_column": "id",
                    },
                },
                {
                    "name": "total",
                    "data_type": "numeric",
                    "nullable": False,
                    "default": None,
                    "ordinal_position": 3,
                    "precision": 10,
                    "scale": 2,
                },
            ],
            "primary_key": ["id"],
        },
    },
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def api_client():
    return APIClient()


@pytest.fixture
def project(db, user):
    """Project with a pre-populated data dictionary."""
    p = Project.objects.create(
        name="Test Project",
        slug="test-project",
        db_schema="public",
        data_dictionary=SAMPLE_DATA_DICTIONARY,
        created_by=user,
    )
    return p


@pytest.fixture
def project_no_dict(db, user):
    """Project with no data dictionary generated yet."""
    return Project.objects.create(
        name="Empty Project",
        slug="empty-project",
        db_schema="public",
        created_by=user,
    )


@pytest.fixture
def admin_membership(db, user, project):
    return ProjectMembership.objects.create(
        user=user, project=project, role=ProjectRole.ADMIN
    )


@pytest.fixture
def analyst_membership(db, user, project):
    return ProjectMembership.objects.create(
        user=user, project=project, role=ProjectRole.ANALYST
    )


@pytest.fixture
def viewer_membership(db, user, project):
    return ProjectMembership.objects.create(
        user=user, project=project, role=ProjectRole.VIEWER
    )


@pytest.fixture
def other_user(db):
    from django.contrib.auth import get_user_model
    return get_user_model().objects.create_user(
        email="other@example.com",
        password="otherpass123",
        first_name="Other",
        last_name="User",
    )


@pytest.fixture
def annotation(db, project, user):
    """Pre-existing TableKnowledge annotation for public.users."""
    return TableKnowledge.objects.create(
        project=project,
        table_name="public.users",
        description="Core user accounts table",
        use_cases=["User retention analysis", "Revenue attribution"],
        data_quality_notes=["email may contain test accounts with @example.com"],
        owner="Platform Team",
        refresh_frequency="Real-time",
        related_tables=[{"table": "orders", "join_hint": "orders.user_id = users.id"}],
        column_notes={"email": "Always lowercase, validated on write"},
        updated_by=user,
    )


# ===========================================================================
# DataDictionaryView — GET /api/projects/{id}/data-dictionary/
# ===========================================================================
class TestDataDictionaryView:
    """Tests for the merged data dictionary endpoint."""

    url_for = staticmethod(
        lambda pid: f"/api/projects/{pid}/data-dictionary/"
    )

    def test_returns_tables_for_member(self, api_client, user, project, viewer_membership):
        """Any project member can fetch the data dictionary."""
        api_client.force_authenticate(user)
        resp = api_client.get(self.url_for(project.id))

        assert resp.status_code == 200
        data = resp.json()
        assert "tables" in data
        assert "public.users" in data["tables"]
        assert "public.orders" in data["tables"]

    def test_merges_annotations(self, api_client, user, project, viewer_membership, annotation):
        """Annotations from TableKnowledge are merged into the response."""
        api_client.force_authenticate(user)
        resp = api_client.get(self.url_for(project.id))

        assert resp.status_code == 200
        users_table = resp.json()["tables"]["public.users"]
        assert "annotation" in users_table
        ann = users_table["annotation"]
        assert ann["description"] == "Core user accounts table"
        assert ann["owner"] == "Platform Team"
        assert ann["refresh_frequency"] == "Real-time"

    def test_table_without_annotation_has_no_annotation_key(
        self, api_client, user, project, viewer_membership, annotation
    ):
        """Tables with no TableKnowledge record should not have an annotation key."""
        api_client.force_authenticate(user)
        resp = api_client.get(self.url_for(project.id))

        orders_table = resp.json()["tables"]["public.orders"]
        assert "annotation" not in orders_table

    def test_empty_dictionary_returns_empty_tables(
        self, api_client, user, project_no_dict
    ):
        """A project with no data dictionary returns empty tables."""
        ProjectMembership.objects.create(
            user=user, project=project_no_dict, role=ProjectRole.VIEWER
        )
        api_client.force_authenticate(user)
        resp = api_client.get(self.url_for(project_no_dict.id))

        assert resp.status_code == 200
        assert resp.json()["tables"] == {}
        assert resp.json()["generated_at"] is None

    def test_non_member_gets_403(self, api_client, other_user, project):
        """Users not in the project membership get 403."""
        api_client.force_authenticate(other_user)
        resp = api_client.get(self.url_for(project.id))
        assert resp.status_code == 403

    def test_unauthenticated_gets_403(self, api_client, project):
        """Unauthenticated requests get 403."""
        resp = api_client.get(self.url_for(project.id))
        assert resp.status_code == 403

    def test_nonexistent_project_gets_404(self, api_client, user):
        """Request for a non-existent project returns 404."""
        api_client.force_authenticate(user)
        resp = api_client.get(self.url_for(uuid.uuid4()))
        assert resp.status_code == 404

    def test_superuser_bypasses_membership(self, api_client, admin_user, project):
        """Django superusers can access any project without membership."""
        api_client.force_authenticate(admin_user)
        resp = api_client.get(self.url_for(project.id))
        assert resp.status_code == 200

    def test_columns_are_included(self, api_client, user, project, viewer_membership):
        """Table column metadata is included in the response."""
        api_client.force_authenticate(user)
        resp = api_client.get(self.url_for(project.id))

        users_table = resp.json()["tables"]["public.users"]
        columns = users_table["columns"]
        assert len(columns) == 3
        col_names = [c["name"] for c in columns]
        assert "id" in col_names
        assert "email" in col_names
        assert "created_at" in col_names


# ===========================================================================
# TableAnnotationsView — GET /api/projects/{id}/data-dictionary/tables/{path}/
# ===========================================================================
class TestTableAnnotationsViewGet:
    """Tests for fetching a single table's detail + annotations."""

    url_for = staticmethod(
        lambda pid, table_path: f"/api/projects/{pid}/data-dictionary/tables/{table_path}/"
    )

    def test_get_table_with_annotation(
        self, api_client, user, project, viewer_membership, annotation
    ):
        """GET returns table schema merged with annotation."""
        api_client.force_authenticate(user)
        resp = api_client.get(self.url_for(project.id, "public.users"))

        assert resp.status_code == 200
        data = resp.json()
        assert data["schema"] == "public"
        assert data["name"] == "users"
        assert data["qualified_name"] == "public.users"
        assert data["annotation"]["description"] == "Core user accounts table"
        assert data["annotation"]["column_notes"]["email"] == "Always lowercase, validated on write"

    def test_get_table_without_annotation(
        self, api_client, user, project, viewer_membership
    ):
        """GET returns table schema without annotation key when none exists."""
        api_client.force_authenticate(user)
        resp = api_client.get(self.url_for(project.id, "public.orders"))

        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "orders"
        assert "annotation" not in data

    def test_get_nonexistent_table_returns_404(
        self, api_client, user, project, viewer_membership
    ):
        """Requesting a table not in the dictionary returns 404."""
        api_client.force_authenticate(user)
        resp = api_client.get(self.url_for(project.id, "public.nonexistent"))
        assert resp.status_code == 404

    def test_get_defaults_to_public_schema(
        self, api_client, user, project, viewer_membership
    ):
        """Table path without schema prefix defaults to public."""
        api_client.force_authenticate(user)
        # "users" should resolve to "public.users"
        resp = api_client.get(self.url_for(project.id, "users"))
        assert resp.status_code == 200
        assert resp.json()["qualified_name"] == "public.users"

    def test_get_rejects_sql_injection_in_table_path(
        self, api_client, user, project, viewer_membership
    ):
        """Malicious table path characters are rejected with 400."""
        api_client.force_authenticate(user)
        resp = api_client.get(self.url_for(project.id, "public.users;DROP TABLE"))
        assert resp.status_code == 400

    def test_non_member_gets_403(self, api_client, other_user, project):
        api_client.force_authenticate(other_user)
        resp = api_client.get(self.url_for(project.id, "public.users"))
        assert resp.status_code == 403


# ===========================================================================
# TableAnnotationsView — PUT /api/projects/{id}/data-dictionary/tables/{path}/
# ===========================================================================
class TestTableAnnotationsViewPut:
    """Tests for creating/updating table annotations."""

    url_for = staticmethod(
        lambda pid, table_path: f"/api/projects/{pid}/data-dictionary/tables/{table_path}/"
    )

    def test_admin_can_create_annotation(
        self, api_client, user, project, admin_membership
    ):
        """Admin can create a new TableKnowledge annotation."""
        api_client.force_authenticate(user)
        payload = {
            "description": "All customer orders",
            "use_cases": "Revenue analysis",
            "owner": "Data Team",
        }
        resp = api_client.put(
            self.url_for(project.id, "public.orders"),
            data=payload,
            format="json",
        )

        assert resp.status_code == 201
        data = resp.json()
        assert data["description"] == "All customer orders"
        assert data["owner"] == "Data Team"
        assert data["created"] is True

        # Verify persisted in DB
        tk = TableKnowledge.objects.get(project=project, table_name="public.orders")
        assert tk.description == "All customer orders"
        assert tk.updated_by == user

    def test_admin_can_update_existing_annotation(
        self, api_client, user, project, admin_membership, annotation
    ):
        """Admin can update an existing TableKnowledge annotation."""
        api_client.force_authenticate(user)
        payload = {
            "description": "Updated user accounts table description",
            "owner": "Identity Team",
        }
        resp = api_client.put(
            self.url_for(project.id, "public.users"),
            data=payload,
            format="json",
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["description"] == "Updated user accounts table description"
        assert data["owner"] == "Identity Team"
        assert data["created"] is False

    def test_put_requires_description(
        self, api_client, user, project, admin_membership
    ):
        """PUT without description returns 400."""
        api_client.force_authenticate(user)
        payload = {"owner": "Data Team"}  # no description
        resp = api_client.put(
            self.url_for(project.id, "public.orders"),
            data=payload,
            format="json",
        )
        assert resp.status_code == 400
        assert "description" in resp.json()["error"].lower()

    def test_put_rejects_nonexistent_table(
        self, api_client, user, project, admin_membership
    ):
        """Cannot annotate a table not in the data dictionary."""
        api_client.force_authenticate(user)
        payload = {"description": "Ghost table"}
        resp = api_client.put(
            self.url_for(project.id, "public.nonexistent"),
            data=payload,
            format="json",
        )
        assert resp.status_code == 404

    def test_viewer_cannot_put(
        self, api_client, user, project, viewer_membership
    ):
        """Viewers are not allowed to write annotations."""
        api_client.force_authenticate(user)
        payload = {"description": "Should not work"}
        resp = api_client.put(
            self.url_for(project.id, "public.users"),
            data=payload,
            format="json",
        )
        assert resp.status_code == 403

    def test_analyst_cannot_put(
        self, api_client, user, project, analyst_membership
    ):
        """Analysts are not allowed to write annotations (admin only)."""
        api_client.force_authenticate(user)
        payload = {"description": "Should not work either"}
        resp = api_client.put(
            self.url_for(project.id, "public.users"),
            data=payload,
            format="json",
        )
        assert resp.status_code == 403

    def test_superuser_can_put_without_membership(
        self, api_client, admin_user, project
    ):
        """Superusers can annotate tables without project membership."""
        api_client.force_authenticate(admin_user)
        payload = {"description": "Superuser annotation"}
        resp = api_client.put(
            self.url_for(project.id, "public.orders"),
            data=payload,
            format="json",
        )
        assert resp.status_code == 201

    def test_put_saves_column_notes(
        self, api_client, user, project, admin_membership
    ):
        """Column-level notes are saved correctly."""
        api_client.force_authenticate(user)
        payload = {
            "description": "Order data",
            "column_notes": {
                "total": "Amount in USD cents",
                "user_id": "FK to users.id",
            },
        }
        resp = api_client.put(
            self.url_for(project.id, "public.orders"),
            data=payload,
            format="json",
        )

        assert resp.status_code == 201
        assert resp.json()["column_notes"]["total"] == "Amount in USD cents"

        tk = TableKnowledge.objects.get(project=project, table_name="public.orders")
        assert tk.column_notes["user_id"] == "FK to users.id"

    def test_put_ignores_unknown_fields(
        self, api_client, user, project, admin_membership
    ):
        """Unknown fields in the payload are silently ignored."""
        api_client.force_authenticate(user)
        payload = {
            "description": "Valid description",
            "unknown_field": "should be ignored",
            "another_bad_field": 42,
        }
        resp = api_client.put(
            self.url_for(project.id, "public.orders"),
            data=payload,
            format="json",
        )
        assert resp.status_code == 201
        # Response should not contain unknown fields
        data = resp.json()
        assert "unknown_field" not in data
        assert "another_bad_field" not in data


# ===========================================================================
# RefreshSchemaView — POST /api/projects/{id}/refresh-schema/
# ===========================================================================
class TestRefreshSchemaView:
    """Tests for the schema refresh endpoint (permission checks; DB mocked)."""

    url_for = staticmethod(
        lambda pid: f"/api/projects/{pid}/refresh-schema/"
    )

    def test_viewer_cannot_refresh(
        self, api_client, user, project, viewer_membership
    ):
        """Only admins can trigger a schema refresh."""
        api_client.force_authenticate(user)
        resp = api_client.post(self.url_for(project.id))
        assert resp.status_code == 403

    def test_analyst_cannot_refresh(
        self, api_client, user, project, analyst_membership
    ):
        api_client.force_authenticate(user)
        resp = api_client.post(self.url_for(project.id))
        assert resp.status_code == 403

    def test_non_member_cannot_refresh(self, api_client, other_user, project):
        api_client.force_authenticate(other_user)
        resp = api_client.post(self.url_for(project.id))
        assert resp.status_code == 403

    def test_unauthenticated_cannot_refresh(self, api_client, project):
        resp = api_client.post(self.url_for(project.id))
        assert resp.status_code == 403

    def test_admin_can_refresh_mocked(
        self, api_client, user, project, admin_membership, mocker
    ):
        """Admin triggers refresh; DB connection mocked to avoid real introspection."""
        mock_fetch = mocker.patch(
            "apps.projects.api.data_dictionary.RefreshSchemaView._fetch_schema",
            return_value=SAMPLE_DATA_DICTIONARY,
        )
        api_client.force_authenticate(user)
        resp = api_client.post(self.url_for(project.id))

        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["tables_count"] == 2
        assert "generated_at" in data

        # Verify project was updated
        project.refresh_from_db()
        assert project.data_dictionary is not None
        assert project.data_dictionary_generated_at is not None
        mock_fetch.assert_awaited_once()

    def test_refresh_failure_returns_500(
        self, api_client, user, project, admin_membership, mocker
    ):
        """If the DB introspection fails, 500 is returned with error message."""
        mocker.patch(
            "apps.projects.api.data_dictionary.RefreshSchemaView._fetch_schema",
            side_effect=Exception("Connection refused"),
        )
        api_client.force_authenticate(user)
        resp = api_client.post(self.url_for(project.id))

        assert resp.status_code == 500
        assert "Connection refused" in resp.json()["error"]


# ===========================================================================
# TableKnowledge model — unit-level checks
# ===========================================================================
class TestTableKnowledgeModel:
    """Unit tests for the TableKnowledge model."""

    def test_unique_together_constraint(self, db, project, user):
        """Cannot create two annotations for the same table in a project."""
        TableKnowledge.objects.create(
            project=project,
            table_name="public.users",
            description="First",
            updated_by=user,
        )
        with pytest.raises(Exception):  # IntegrityError
            TableKnowledge.objects.create(
                project=project,
                table_name="public.users",
                description="Duplicate",
                updated_by=user,
            )

    def test_default_json_fields(self, db, project, user):
        """JSON fields default to empty list/dict."""
        tk = TableKnowledge.objects.create(
            project=project,
            table_name="public.test",
            description="Test table",
            updated_by=user,
        )
        assert tk.use_cases == []
        assert tk.data_quality_notes == []
        assert tk.related_tables == []
        assert tk.column_notes == {}

    def test_str_representation(self, db, project, user):
        tk = TableKnowledge.objects.create(
            project=project,
            table_name="public.users",
            description="Users",
            updated_by=user,
        )
        assert "public.users" in str(tk)
        assert project.name in str(tk)
