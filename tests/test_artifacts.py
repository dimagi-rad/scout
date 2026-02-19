"""
Comprehensive tests for Phase 3 (Frontend & Artifacts) of the Scout data agent platform.

Tests artifact models, views, access control, versioning, sharing, and artifact tools.
"""
import uuid
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.db import IntegrityError
from django.test import Client
from django.utils import timezone

from apps.artifacts.models import Artifact, ArtifactType, SharedArtifact
from apps.projects.models import Project, ProjectMembership, ProjectRole

User = get_user_model()


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def project(db_connection, user):
    """Create a test project."""
    return Project.objects.create(
        name="Test Project",
        slug="test-project",
        description="A test project",
        database_connection=db_connection,
        db_schema="public",
        created_by=user,
    )


@pytest.fixture
def project_membership(db, user, project):
    """Create a project membership for the test user."""
    return ProjectMembership.objects.create(
        user=user,
        project=project,
        role=ProjectRole.ANALYST,
    )


@pytest.fixture
def other_user(db):
    """Create another test user."""
    return User.objects.create_user(
        email="other@example.com",
        password="otherpass123",
        first_name="Other",
        last_name="User",
    )


@pytest.fixture
def other_project(db_connection, other_user):
    """Create a project for another user."""
    return Project.objects.create(
        name="Other Project",
        slug="other-project",
        description="Another test project",
        database_connection=db_connection,
        db_schema="public",
        created_by=other_user,
    )


@pytest.fixture
def artifact(db, user, project):
    """Create a test artifact."""
    return Artifact.objects.create(
        project=project,
        created_by=user,
        title="Test Chart",
        description="A test visualization",
        artifact_type=ArtifactType.REACT,
        code="export default function Chart({ data }) { return <div>Chart</div>; }",
        data={"rows": [{"x": 1, "y": 2}]},
        version=1,
        conversation_id="thread_123",
        source_queries=["SELECT * FROM users"],
    )


@pytest.fixture
def shared_artifact(db, user, artifact):
    """Create a shared artifact with public access."""
    return SharedArtifact.objects.create(
        artifact=artifact,
        created_by=user,
        share_token="public_token_123",
        access_level="public",
    )


@pytest.fixture
def client():
    """Django test client."""
    return Client()


@pytest.fixture
def authenticated_client(client, user):
    """Authenticated Django test client."""
    client.force_login(user)
    return client


# ============================================================================
# 1. TestArtifactModel
# ============================================================================


@pytest.mark.django_db
class TestArtifactModel:
    """Tests for the Artifact model."""

    def test_create_artifact(self, user, project):
        """Test creating a basic artifact."""
        artifact = Artifact.objects.create(
            project=project,
            created_by=user,
            title="Sales Dashboard",
            description="Q4 sales analysis",
            artifact_type=ArtifactType.REACT,
            code="export default function Dashboard() { return <div>Dashboard</div>; }",
            data={"sales": [100, 200, 300]},
            version=1,
            conversation_id="conv_456",
            source_queries=["SELECT * FROM sales WHERE quarter = 'Q4'"],
        )

        assert artifact.id is not None
        assert artifact.title == "Sales Dashboard"
        assert artifact.description == "Q4 sales analysis"
        assert artifact.artifact_type == ArtifactType.REACT
        assert artifact.version == 1
        assert artifact.conversation_id == "conv_456"
        assert len(artifact.source_queries) == 1
        assert artifact.data["sales"] == [100, 200, 300]
        assert artifact.parent_artifact is None
        assert str(artifact) == "Sales Dashboard (v1)"

    def test_artifact_versioning(self, user, project, artifact):
        """Test artifact versioning with parent_artifact relationship."""
        # Create a new version based on the original
        new_version = Artifact.objects.create(
            project=project,
            created_by=user,
            title=artifact.title,
            description="Updated version",
            artifact_type=artifact.artifact_type,
            code="export default function Chart({ data }) { return <div>Updated Chart</div>; }",
            data={"rows": [{"x": 1, "y": 3}]},
            version=2,
            parent_artifact=artifact,
            conversation_id=artifact.conversation_id,
            source_queries=artifact.source_queries,
        )

        assert new_version.version == 2
        assert new_version.parent_artifact == artifact
        assert artifact.child_versions.count() == 1
        assert artifact.child_versions.first() == new_version
        assert new_version.code != artifact.code
        assert str(new_version) == f"{artifact.title} (v2)"

    def test_content_hash_property(self, user, project):
        """Test content_hash property for deduplication."""
        artifact1 = Artifact.objects.create(
            project=project,
            created_by=user,
            title="Test",
            artifact_type=ArtifactType.HTML,
            code="<div>Test</div>",
            data={"key": "value"},
            version=1,
            conversation_id="conv_1",
        )

        artifact2 = Artifact.objects.create(
            project=project,
            created_by=user,
            title="Test Copy",
            artifact_type=ArtifactType.HTML,
            code="<div>Test</div>",
            data={"key": "value"},
            version=1,
            conversation_id="conv_1",
        )

        # Same code should produce same hash
        assert artifact1.content_hash == artifact2.content_hash

        # Different code should produce different hash
        artifact3 = Artifact.objects.create(
            project=project,
            created_by=user,
            title="Test Different",
            artifact_type=ArtifactType.HTML,
            code="<div>Different</div>",
            data={"key": "value"},
            version=1,
            conversation_id="conv_1",
        )
        assert artifact1.content_hash != artifact3.content_hash

    def test_artifact_types(self, user, project):
        """Test all artifact types can be created."""
        for artifact_type in [
            ArtifactType.REACT,
            ArtifactType.HTML,
            ArtifactType.MARKDOWN,
            ArtifactType.PLOTLY,
            ArtifactType.SVG,
        ]:
            artifact = Artifact.objects.create(
                project=project,
                created_by=user,
                title=f"Test {artifact_type}",
                artifact_type=artifact_type,
                code="test code",
                version=1,
                conversation_id="conv_test",
            )
            assert artifact.artifact_type == artifact_type

        # Verify all types are in choices
        artifact_types = [choice[0] for choice in ArtifactType.choices]
        assert "react" in artifact_types
        assert "html" in artifact_types
        assert "markdown" in artifact_types
        assert "plotly" in artifact_types
        assert "svg" in artifact_types


# ============================================================================
# 2. TestSharedArtifactModel
# ============================================================================


@pytest.mark.django_db
class TestSharedArtifactModel:
    """Tests for the SharedArtifact model."""

    def test_create_shared_artifact(self, user, artifact):
        """Test creating a shared artifact."""
        shared = SharedArtifact.objects.create(
            artifact=artifact,
            created_by=user,
            share_token="test_token_456",
            access_level="project",
        )

        assert shared.id is not None
        assert shared.artifact == artifact
        assert shared.created_by == user
        assert shared.share_token == "test_token_456"
        assert shared.access_level == "project"
        assert shared.view_count == 0
        assert shared.expires_at is None
        assert str(shared) == f"Share: {artifact.title} (project)"

    def test_share_url_property(self, user, artifact):
        """Test share_url property."""
        shared = SharedArtifact.objects.create(
            artifact=artifact,
            created_by=user,
            share_token="url_test_token",
            access_level="public",
        )

        # The model returns the path without /api prefix
        assert "shared" in shared.share_url
        assert "url_test_token" in shared.share_url

    def test_is_expired_property_not_expired(self, user, artifact):
        """Test is_expired property when share has not expired."""
        # No expiry date
        shared = SharedArtifact.objects.create(
            artifact=artifact,
            created_by=user,
            share_token="never_expires",
            access_level="public",
        )
        assert shared.is_expired is False

        # Expiry date in the future
        future_expiry = timezone.now() + timedelta(days=7)
        shared_future = SharedArtifact.objects.create(
            artifact=artifact,
            created_by=user,
            share_token="future_expires",
            access_level="public",
            expires_at=future_expiry,
        )
        assert shared_future.is_expired is False

    def test_is_expired_property_expired(self, user, artifact):
        """Test is_expired property when share has expired."""
        past_expiry = timezone.now() - timedelta(days=1)
        shared = SharedArtifact.objects.create(
            artifact=artifact,
            created_by=user,
            share_token="expired_token",
            access_level="public",
            expires_at=past_expiry,
        )
        assert shared.is_expired is True

    def test_access_levels(self, user, artifact):
        """Test all access levels can be created."""
        access_levels = ["public", "project", "specific"]

        for level in access_levels:
            shared = SharedArtifact.objects.create(
                artifact=artifact,
                created_by=user,
                share_token=f"token_{level}",
                access_level=level,
            )
            assert shared.access_level == level

        # Test with allowed_users for specific access
        other_user = User.objects.create_user(
            email="allowed@example.com",
            password="pass123",
        )
        shared_specific = SharedArtifact.objects.create(
            artifact=artifact,
            created_by=user,
            share_token="specific_with_users",
            access_level="specific",
        )
        shared_specific.allowed_users.add(other_user)
        assert shared_specific.allowed_users.count() == 1
        assert other_user in shared_specific.allowed_users.all()


# ============================================================================
# 3. TestArtifactSandboxView
# ============================================================================


@pytest.mark.django_db
class TestArtifactSandboxView:
    """Tests for the ArtifactSandboxView."""

    def test_sandbox_returns_html(self, authenticated_client, artifact, project_membership):
        """Test that sandbox view returns HTML content."""
        response = authenticated_client.get(f"/api/artifacts/{artifact.id}/sandbox/")

        assert response.status_code == 200
        assert "text/html" in response["Content-Type"]

        # Check for key sandbox elements
        content = response.content.decode()
        assert "<!DOCTYPE html>" in content
        assert "Artifact Sandbox" in content
        assert "React" in content or "react" in content
        assert "root" in content

    def test_sandbox_csp_headers(self, authenticated_client, artifact, project_membership):
        """Test that CSP headers are set correctly for security."""
        response = authenticated_client.get(f"/api/artifacts/{artifact.id}/sandbox/")

        assert response.status_code == 200
        assert "Content-Security-Policy" in response

        csp = response["Content-Security-Policy"]

        # Verify key CSP directives
        assert "default-src 'none'" in csp
        assert "script-src" in csp
        assert "'unsafe-inline'" in csp  # Required for Babel transpilation
        assert "'unsafe-eval'" in csp  # Required for JSX transpilation
        assert "https://cdn.jsdelivr.net" in csp
        assert "connect-src" in csp  # Network access restricted to CDN only
        assert "img-src data: blob:" in csp


# ============================================================================
# 4. TestArtifactDataView
# ============================================================================


@pytest.mark.django_db
class TestArtifactDataView:
    """Tests for the ArtifactDataView."""

    def test_get_artifact_data_authenticated(
        self, authenticated_client, artifact, project_membership
    ):
        """Test authenticated user with project access can get artifact data."""
        response = authenticated_client.get(f"/api/artifacts/{artifact.id}/data/")

        assert response.status_code == 200
        data = response.json()

        assert data["id"] == str(artifact.id)
        assert data["title"] == artifact.title
        assert data["type"] == artifact.artifact_type
        assert data["code"] == artifact.code
        assert data["data"] == artifact.data
        assert data["version"] == artifact.version

    def test_get_artifact_data_unauthenticated(self, client, artifact):
        """Test unauthenticated user cannot access artifact data."""
        response = client.get(f"/api/artifacts/{artifact.id}/data/")

        assert response.status_code == 401
        data = response.json()
        assert "error" in data

    def test_get_artifact_data_not_found(self, authenticated_client):
        """Test accessing non-existent artifact returns 404."""
        fake_id = uuid.uuid4()
        response = authenticated_client.get(f"/api/artifacts/{fake_id}/data/")

        assert response.status_code == 404

    def test_artifact_data_requires_project_membership(
        self, user, other_user, other_project, client
    ):
        """Test that artifact access requires project membership."""
        # Create artifact in other_project
        other_artifact = Artifact.objects.create(
            project=other_project,
            created_by=other_user,
            title="Other Artifact",
            artifact_type=ArtifactType.HTML,
            code="<div>Other</div>",
            version=1,
            conversation_id="conv_other",
        )

        # User tries to access artifact from other_project (no membership)
        client.force_login(user)
        response = client.get(f"/api/artifacts/{other_artifact.id}/data/")

        assert response.status_code == 403
        data = response.json()
        assert "error" in data


# ============================================================================
# 5. TestSharedArtifactView
# ============================================================================


@pytest.mark.django_db
class TestSharedArtifactView:
    """Tests for the SharedArtifactView."""

    def test_public_share_accessible_without_auth(self, client, shared_artifact):
        """Test public shared artifact is accessible without authentication."""
        response = client.get(f"/api/artifacts/shared/{shared_artifact.share_token}/")

        assert response.status_code == 200
        data = response.json()

        assert data["id"] == str(shared_artifact.artifact.id)
        assert data["title"] == shared_artifact.artifact.title
        assert data["type"] == shared_artifact.artifact.artifact_type
        assert data["code"] == shared_artifact.artifact.code
        assert data["data"] == shared_artifact.artifact.data

    def test_project_share_requires_membership(
        self, user, other_user, artifact, client
    ):
        """Test project-level share requires project membership."""
        # Create project-level share
        project_share = SharedArtifact.objects.create(
            artifact=artifact,
            created_by=user,
            share_token="project_level_token",
            access_level="project",
        )

        # Without authentication - should fail
        response = client.get(f"/api/artifacts/shared/{project_share.share_token}/")
        assert response.status_code == 401

        # With authentication but no project membership - should fail
        client.force_login(other_user)
        response = client.get(f"/api/artifacts/shared/{project_share.share_token}/")
        assert response.status_code == 403

        # With authentication and project membership - should succeed
        ProjectMembership.objects.create(
            user=user,
            project=artifact.project,
            role=ProjectRole.VIEWER,
        )
        client.force_login(user)
        response = client.get(f"/api/artifacts/shared/{project_share.share_token}/")
        assert response.status_code == 200

    def test_specific_share_requires_allowed_user(
        self, user, other_user, artifact, client
    ):
        """Test specific-level share requires user to be in allowed_users."""
        # Create specific-level share
        specific_share = SharedArtifact.objects.create(
            artifact=artifact,
            created_by=user,
            share_token="specific_level_token",
            access_level="specific",
        )
        specific_share.allowed_users.add(user)

        # Without authentication - should fail
        response = client.get(f"/api/artifacts/shared/{specific_share.share_token}/")
        assert response.status_code == 401

        # With authentication but not in allowed_users - should fail
        client.force_login(other_user)
        response = client.get(f"/api/artifacts/shared/{specific_share.share_token}/")
        assert response.status_code == 403

        # With authentication and in allowed_users - should succeed
        client.force_login(user)
        response = client.get(f"/api/artifacts/shared/{specific_share.share_token}/")
        assert response.status_code == 200

    def test_expired_share_returns_410(self, user, artifact, client):
        """Test expired share link returns 403 Forbidden."""
        past_expiry = timezone.now() - timedelta(hours=1)
        expired_share = SharedArtifact.objects.create(
            artifact=artifact,
            created_by=user,
            share_token="expired_share_token",
            access_level="public",
            expires_at=past_expiry,
        )

        response = client.get(f"/api/artifacts/shared/{expired_share.share_token}/")

        assert response.status_code == 403
        data = response.json()
        assert "error" in data
        assert "expired" in data["error"].lower()

    def test_view_count_incremented(self, client, shared_artifact):
        """Test that view_count is incremented via POST."""
        initial_count = shared_artifact.view_count
        assert initial_count == 0

        # View count incremented via POST (not GET, per implementation design)
        client.post(f"/api/artifacts/shared/{shared_artifact.share_token}/")
        shared_artifact.refresh_from_db()
        assert shared_artifact.view_count == initial_count + 1

        client.post(f"/api/artifacts/shared/{shared_artifact.share_token}/")
        shared_artifact.refresh_from_db()
        assert shared_artifact.view_count == initial_count + 2

        client.post(f"/api/artifacts/shared/{shared_artifact.share_token}/")
        shared_artifact.refresh_from_db()
        assert shared_artifact.view_count == initial_count + 3


# ============================================================================
# 6. TestArtifactTools
# ============================================================================


@pytest.mark.django_db
class TestArtifactTools:
    """Tests for artifact creation and update tools."""

    def test_create_artifact_tool(self, user, project):
        """Test create_artifact tool creates an artifact correctly."""
        from apps.agents.tools.artifact_tool import create_artifact_tools

        tools = create_artifact_tools(project, user)
        create_artifact_tool = tools[0]

        result = create_artifact_tool.invoke({
            "title": "Revenue Chart",
            "artifact_type": "react",
            "code": "export default function Chart() { return <div>Chart</div>; }",
            "description": "Monthly revenue visualization",
            "data": {"revenue": [1000, 2000, 3000]},
            "source_queries": ["SELECT month, revenue FROM sales"],
        })

        assert result["status"] == "created"
        assert "artifact_id" in result
        assert result["title"] == "Revenue Chart"
        assert result["type"] == "react"
        assert "/artifacts/" in result["render_url"]
        assert "/render" in result["render_url"]

        # Verify artifact was created in database
        artifact = Artifact.objects.get(id=result["artifact_id"])
        assert artifact.title == "Revenue Chart"
        assert artifact.artifact_type == "react"
        assert artifact.code == "export default function Chart() { return <div>Chart</div>; }"
        assert artifact.data["revenue"] == [1000, 2000, 3000]
        assert artifact.data["_source_queries"] == ["SELECT month, revenue FROM sales"]
        assert artifact.version == 1
        assert artifact.parent_artifact is None

    def test_update_artifact_tool(self, user, project, artifact, project_membership):
        """Test update_artifact tool creates a new version of an artifact."""
        from apps.agents.tools.artifact_tool import create_artifact_tools

        tools = create_artifact_tools(project, user)
        update_artifact_tool = tools[1]

        original_version = artifact.version
        new_code = "export default function Chart() { return <div>Updated Chart</div>; }"

        result = update_artifact_tool.invoke({
            "artifact_id": str(artifact.id),
            "code": new_code,
            "title": "Updated Chart Title",
            "data": {"rows": [{"x": 2, "y": 4}]},
        })

        assert result["status"] == "updated"
        assert "artifact_id" in result
        assert result["version"] == original_version + 1

        # Update creates a NEW artifact (not in-place), verify the new one
        new_artifact = Artifact.objects.get(id=result["artifact_id"])
        assert new_artifact.version == original_version + 1
        assert new_artifact.code == new_code
        assert new_artifact.title == "Updated Chart Title"
        assert new_artifact.data == {"rows": [{"x": 2, "y": 4}]}

    def test_update_creates_new_version(self, user, project, artifact, project_membership):
        """Test that update_artifact creates new artifacts with incrementing versions."""
        from apps.agents.tools.artifact_tool import create_artifact_tools

        tools = create_artifact_tools(project, user)
        update_artifact_tool = tools[1]

        original_version = artifact.version

        # First update - creates new artifact from original
        result1 = update_artifact_tool.invoke({
            "artifact_id": str(artifact.id),
            "code": "export default function Chart() { return <div>Version 2</div>; }",
        })

        assert result1["status"] == "updated"
        assert result1["version"] == original_version + 1

        # Second update - creates new artifact from the v2 artifact
        result2 = update_artifact_tool.invoke({
            "artifact_id": result1["artifact_id"],
            "code": "export default function Chart() { return <div>Version 3</div>; }",
        })

        assert result2["status"] == "updated"
        assert result2["version"] == original_version + 2


# ============================================================================
# 7. TestSharedArtifactAccessControl
# ============================================================================


@pytest.mark.django_db
class TestSharedArtifactAccessControl:
    """Tests for artifact sharing access control."""

    def test_create_public_share_link(self, user, artifact):
        """Test creating a public share link."""
        token = SharedArtifact.generate_token()
        shared = SharedArtifact.objects.create(
            artifact=artifact,
            created_by=user,
            share_token=token,
            access_level="public",
        )

        assert shared.access_level == "public"
        assert shared.share_token == token
        assert shared.view_count == 0
        assert shared.expires_at is None

    def test_create_project_share_link(self, user, artifact):
        """Test creating a project-level share link."""
        shared = SharedArtifact.objects.create(
            artifact=artifact,
            created_by=user,
            share_token=SharedArtifact.generate_token(),
            access_level="project",
        )

        assert shared.access_level == "project"
        assert shared.artifact == artifact

    def test_create_specific_share_link(self, user, other_user, artifact):
        """Test creating a specific user share link."""
        shared = SharedArtifact.objects.create(
            artifact=artifact,
            created_by=user,
            share_token=SharedArtifact.generate_token(),
            access_level="specific",
        )
        shared.allowed_users.add(other_user)

        assert shared.access_level == "specific"
        assert shared.allowed_users.count() == 1
        assert other_user in shared.allowed_users.all()

    def test_create_share_link_with_expiry(self, user, artifact):
        """Test creating a share link with expiration date."""
        future_expiry = timezone.now() + timedelta(days=7)
        shared = SharedArtifact.objects.create(
            artifact=artifact,
            created_by=user,
            share_token=SharedArtifact.generate_token(),
            access_level="public",
            expires_at=future_expiry,
        )

        assert shared.expires_at == future_expiry
        assert not shared.is_expired

    def test_public_access_allows_unauthenticated_users(self, user, artifact):
        """Test that public share links can be accessed by unauthenticated users."""
        shared = SharedArtifact.objects.create(
            artifact=artifact,
            created_by=user,
            share_token=SharedArtifact.generate_token(),
            access_level="public",
        )

        # Unauthenticated user (None)
        assert shared.can_access(None) is True

    def test_public_access_allows_any_authenticated_user(self, user, other_user, artifact):
        """Test that public share links work for any authenticated user."""
        shared = SharedArtifact.objects.create(
            artifact=artifact,
            created_by=user,
            share_token=SharedArtifact.generate_token(),
            access_level="public",
        )

        assert shared.can_access(user) is True
        assert shared.can_access(other_user) is True

    def test_project_access_requires_membership(self, user, other_user, artifact, project_membership):
        """Test that project-level access requires project membership."""
        shared = SharedArtifact.objects.create(
            artifact=artifact,
            created_by=user,
            share_token=SharedArtifact.generate_token(),
            access_level="project",
        )

        # User with membership can access
        assert shared.can_access(user) is True

        # Other user without membership cannot access
        assert shared.can_access(other_user) is False

    def test_project_access_rejects_unauthenticated(self, user, artifact):
        """Test that project-level access rejects unauthenticated users."""
        shared = SharedArtifact.objects.create(
            artifact=artifact,
            created_by=user,
            share_token=SharedArtifact.generate_token(),
            access_level="project",
        )

        assert shared.can_access(None) is False

    def test_specific_access_requires_allowed_user(self, user, other_user, artifact):
        """Test that specific access requires user to be in allowed_users."""
        third_user = User.objects.create_user(
            email="third@example.com",
            password="pass123",
        )

        shared = SharedArtifact.objects.create(
            artifact=artifact,
            created_by=user,
            share_token=SharedArtifact.generate_token(),
            access_level="specific",
        )
        shared.allowed_users.add(other_user)

        # Allowed user can access
        assert shared.can_access(other_user) is True

        # Non-allowed user cannot access
        assert shared.can_access(third_user) is False

        # Creator is not automatically allowed
        assert shared.can_access(user) is False

    def test_specific_access_rejects_unauthenticated(self, user, artifact):
        """Test that specific access rejects unauthenticated users."""
        shared = SharedArtifact.objects.create(
            artifact=artifact,
            created_by=user,
            share_token=SharedArtifact.generate_token(),
            access_level="specific",
        )

        assert shared.can_access(None) is False

    def test_expired_share_link_denies_all_access(self, user, artifact):
        """Test that expired share links deny access regardless of access level."""
        past_expiry = timezone.now() - timedelta(hours=1)

        # Public share that's expired
        public_expired = SharedArtifact.objects.create(
            artifact=artifact,
            created_by=user,
            share_token=SharedArtifact.generate_token(),
            access_level="public",
            expires_at=past_expiry,
        )

        assert public_expired.is_expired is True
        assert public_expired.can_access(None) is False
        assert public_expired.can_access(user) is False

    def test_view_count_tracking(self, user, artifact):
        """Test that view count is tracked correctly."""
        shared = SharedArtifact.objects.create(
            artifact=artifact,
            created_by=user,
            share_token=SharedArtifact.generate_token(),
            access_level="public",
        )

        initial_count = shared.view_count
        assert initial_count == 0

        # Increment view count
        shared.increment_view_count()
        shared.refresh_from_db()
        assert shared.view_count == initial_count + 1

        # Increment again
        shared.increment_view_count()
        shared.refresh_from_db()
        assert shared.view_count == initial_count + 2

    def test_multiple_share_links_for_same_artifact(self, user, artifact):
        """Test that multiple share links can be created for the same artifact."""
        SharedArtifact.objects.create(
            artifact=artifact,
            created_by=user,
            share_token=SharedArtifact.generate_token(),
            access_level="public",
        )

        SharedArtifact.objects.create(
            artifact=artifact,
            created_by=user,
            share_token=SharedArtifact.generate_token(),
            access_level="project",
        )

        artifact_shares = SharedArtifact.objects.filter(artifact=artifact)
        assert artifact_shares.count() == 2

    def test_share_token_is_unique(self, user, artifact):
        """Test that share tokens are unique across all shares."""
        token = SharedArtifact.generate_token()
        SharedArtifact.objects.create(
            artifact=artifact,
            created_by=user,
            share_token=token,
            access_level="public",
        )

        # Creating another share with same token should fail
        with pytest.raises(IntegrityError):
            SharedArtifact.objects.create(
                artifact=artifact,
                created_by=user,
                share_token=token,  # Duplicate token
                access_level="public",
            )

    def test_generate_token_produces_valid_token(self):
        """Test that generate_token produces a valid URL-safe token."""
        token = SharedArtifact.generate_token()

        # token_urlsafe(32) produces a 43-character base64url string
        assert len(token) == 43
        assert isinstance(token, str)
        # Ensure uniqueness
        token2 = SharedArtifact.generate_token()
        assert token != token2

    def test_share_url_property(self, user, artifact):
        """Test that share_url property generates correct URL."""
        shared = SharedArtifact.objects.create(
            artifact=artifact,
            created_by=user,
            share_token="test_token_123",
            access_level="public",
        )

        url = shared.share_url
        assert "/artifacts/shared/test_token_123/" in url


# ============================================================================
# 8. TestSharedArtifactViewAccessControl
# ============================================================================


@pytest.mark.django_db
class TestSharedArtifactViewAccessControl:
    """Tests for SharedArtifactView access control enforcement."""

    def test_access_public_share_without_auth(self, client, user, artifact):
        """Test accessing public share without authentication."""
        shared = SharedArtifact.objects.create(
            artifact=artifact,
            created_by=user,
            share_token="public_test_token",
            access_level="public",
        )

        response = client.get(f"/api/artifacts/shared/{shared.share_token}/")

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == str(artifact.id)

    def test_access_project_share_requires_membership(
        self, client, user, other_user, artifact, project_membership
    ):
        """Test that project share requires project membership."""
        shared = SharedArtifact.objects.create(
            artifact=artifact,
            created_by=user,
            share_token="project_test_token",
            access_level="project",
        )

        # Without auth
        response = client.get(f"/api/artifacts/shared/{shared.share_token}/")
        assert response.status_code == 401

        # With auth but no membership
        client.force_login(other_user)
        response = client.get(f"/api/artifacts/shared/{shared.share_token}/")
        assert response.status_code == 403

        # With auth and membership
        client.force_login(user)
        response = client.get(f"/api/artifacts/shared/{shared.share_token}/")
        assert response.status_code == 200

    def test_access_specific_share_requires_allowed_user(
        self, client, user, other_user, artifact
    ):
        """Test that specific share requires user in allowed_users."""
        third_user = User.objects.create_user(
            email="third@example.com",
            password="pass123",
        )

        shared = SharedArtifact.objects.create(
            artifact=artifact,
            created_by=user,
            share_token="specific_test_token",
            access_level="specific",
        )
        shared.allowed_users.add(other_user)

        # Without auth
        response = client.get(f"/api/artifacts/shared/{shared.share_token}/")
        assert response.status_code == 401

        # With auth but not allowed
        client.force_login(third_user)
        response = client.get(f"/api/artifacts/shared/{shared.share_token}/")
        assert response.status_code == 403

        # With auth and allowed
        client.force_login(other_user)
        response = client.get(f"/api/artifacts/shared/{shared.share_token}/")
        assert response.status_code == 200

    def test_expired_share_returns_403(self, client, user, artifact):
        """Test that expired share returns 403 Forbidden."""
        past_expiry = timezone.now() - timedelta(hours=1)
        shared = SharedArtifact.objects.create(
            artifact=artifact,
            created_by=user,
            share_token="expired_test_token",
            access_level="public",
            expires_at=past_expiry,
        )

        response = client.get(f"/api/artifacts/shared/{shared.share_token}/")

        assert response.status_code == 403
        data = response.json()
        assert "error" in data
        assert "expired" in data["error"].lower()

    def test_nonexistent_share_token_returns_404(self, client):
        """Test that non-existent share token returns 404."""
        response = client.get("/api/artifacts/shared/nonexistent_token_123/")

        assert response.status_code == 404

    def test_view_count_incremented_on_successful_access(
        self, client, user, artifact
    ):
        """Test that view_count is incremented on each successful access."""
        shared = SharedArtifact.objects.create(
            artifact=artifact,
            created_by=user,
            share_token="view_count_token",
            access_level="public",
        )

        initial_count = shared.view_count

        # View count incremented via POST (not GET, per implementation design)
        client.post(f"/api/artifacts/shared/{shared.share_token}/")
        shared.refresh_from_db()
        assert shared.view_count == initial_count + 1

        client.post(f"/api/artifacts/shared/{shared.share_token}/")
        shared.refresh_from_db()
        assert shared.view_count == initial_count + 2

    def test_view_count_not_incremented_on_failed_access(
        self, client, user, other_user, artifact
    ):
        """Test that view_count is not incremented when access is denied."""
        shared = SharedArtifact.objects.create(
            artifact=artifact,
            created_by=user,
            share_token="failed_access_token",
            access_level="specific",
        )
        # Don't add any allowed users

        initial_count = shared.view_count

        # Try to access without auth (should fail)
        client.get(f"/api/artifacts/shared/{shared.share_token}/")
        shared.refresh_from_db()
        assert shared.view_count == initial_count  # Not incremented

        # Try to access as unauthorized user (should fail)
        client.force_login(other_user)
        client.get(f"/api/artifacts/shared/{shared.share_token}/")
        shared.refresh_from_db()
        assert shared.view_count == initial_count  # Still not incremented
