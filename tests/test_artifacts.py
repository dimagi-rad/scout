"""
Comprehensive tests for Phase 3 (Frontend & Artifacts) of the Scout data agent platform.

Tests artifact models, views, access control, versioning, and artifact tools.
"""

import uuid

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.artifacts.models import Artifact, ArtifactType

User = get_user_model()


# ============================================================================
# Fixtures
# ============================================================================


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
def artifact(db, user, workspace):
    """Create a test artifact."""
    return Artifact.objects.create(
        workspace=workspace,
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

    def test_create_artifact(self, user, workspace):
        """Test creating a basic artifact."""
        artifact = Artifact.objects.create(
            workspace=workspace,
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

    def test_artifact_versioning(self, user, workspace, artifact):
        """Test artifact versioning with parent_artifact relationship."""
        # Create a new version based on the original
        new_version = Artifact.objects.create(
            workspace=workspace,
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

    def test_content_hash_property(self, user, workspace):
        """Test content_hash property for deduplication."""
        artifact1 = Artifact.objects.create(
            workspace=workspace,
            created_by=user,
            title="Test",
            artifact_type=ArtifactType.HTML,
            code="<div>Test</div>",
            data={"key": "value"},
            version=1,
            conversation_id="conv_1",
        )

        artifact2 = Artifact.objects.create(
            workspace=workspace,
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
            workspace=workspace,
            created_by=user,
            title="Test Different",
            artifact_type=ArtifactType.HTML,
            code="<div>Different</div>",
            data={"key": "value"},
            version=1,
            conversation_id="conv_1",
        )
        assert artifact1.content_hash != artifact3.content_hash

    def test_artifact_types(self, user, workspace):
        """Test all artifact types can be created."""
        for artifact_type in [
            ArtifactType.REACT,
            ArtifactType.HTML,
            ArtifactType.MARKDOWN,
            ArtifactType.PLOTLY,
            ArtifactType.SVG,
        ]:
            artifact = Artifact.objects.create(
                workspace=workspace,
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
# 3. TestArtifactSandboxView
# ============================================================================


@pytest.mark.django_db
class TestArtifactSandboxView:
    """Tests for the ArtifactSandboxView."""

    def test_sandbox_returns_html(self, authenticated_client, artifact, tenant_membership):
        """Test that sandbox view returns HTML content."""
        response = authenticated_client.get(
            f"/api/artifacts/{tenant_membership.id}/{artifact.id}/sandbox/"
        )

        assert response.status_code == 200
        assert "text/html" in response["Content-Type"]

        # Check for key sandbox elements
        content = response.content.decode()
        assert "<!DOCTYPE html>" in content
        assert "Artifact Sandbox" in content
        assert "React" in content or "react" in content
        assert "root" in content

    def test_sandbox_csp_headers(self, authenticated_client, artifact, tenant_membership):
        """Test that CSP headers are set correctly for security."""
        response = authenticated_client.get(
            f"/api/artifacts/{tenant_membership.id}/{artifact.id}/sandbox/"
        )

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
        self, authenticated_client, artifact, tenant_membership
    ):
        """Test authenticated user with workspace access can get artifact data."""
        response = authenticated_client.get(
            f"/api/artifacts/{tenant_membership.id}/{artifact.id}/data/"
        )

        assert response.status_code == 200
        data = response.json()

        assert data["id"] == str(artifact.id)
        assert data["title"] == artifact.title
        assert data["type"] == artifact.artifact_type
        assert data["code"] == artifact.code
        assert data["data"] == artifact.data
        assert data["version"] == artifact.version

    def test_get_artifact_data_unauthenticated(self, client, artifact, tenant_membership):
        """Test unauthenticated user cannot access artifact data."""
        response = client.get(f"/api/artifacts/{tenant_membership.id}/{artifact.id}/data/")

        assert response.status_code == 401
        data = response.json()
        assert "error" in data

    def test_get_artifact_data_not_found(self, authenticated_client, tenant_membership):
        """Test accessing non-existent artifact returns 404."""
        fake_id = uuid.uuid4()
        response = authenticated_client.get(
            f"/api/artifacts/{tenant_membership.id}/{fake_id}/data/"
        )

        assert response.status_code == 404

    def test_artifact_data_requires_workspace_membership(self, db, user, client):
        """Test that artifact access requires workspace membership (foreign tenant_id -> 403)."""
        from apps.projects.models import TenantWorkspace
        from apps.users.models import Tenant, TenantMembership

        # Create a workspace and membership owned by a different user
        other_user = User.objects.create_user(email="other2@example.com", password="pass")
        other_tenant = Tenant.objects.create(
            provider="commcare", external_id="other-domain", canonical_name="Other Domain"
        )
        other_workspace = TenantWorkspace.objects.create(tenant=other_tenant)
        other_membership = TenantMembership.objects.create(user=other_user, tenant=other_tenant)
        other_artifact = Artifact.objects.create(
            workspace=other_workspace,
            created_by=other_user,
            title="Other Artifact",
            artifact_type=ArtifactType.HTML,
            code="<div>Other</div>",
            version=1,
            conversation_id="conv_other",
        )

        # User tries to access artifact using a tenant_id they don't own -> 403
        client.force_login(user)
        response = client.get(f"/api/artifacts/{other_membership.id}/{other_artifact.id}/data/")

        assert response.status_code == 403
        data = response.json()
        assert "error" in data


# ============================================================================
# 6. TestArtifactTools
# ============================================================================


@pytest.mark.django_db(transaction=True)
class TestArtifactTools:
    """Tests for artifact creation and update tools."""

    @pytest.mark.asyncio
    async def test_create_artifact_tool(self, user, workspace):
        """Test create_artifact tool creates an artifact correctly."""
        from apps.agents.tools.artifact_tool import create_artifact_tools

        tools = create_artifact_tools(workspace, user)
        create_artifact_tool = tools[0]

        result = await create_artifact_tool.ainvoke(
            {
                "title": "Revenue Chart",
                "artifact_type": "react",
                "code": "export default function Chart() { return <div>Chart</div>; }",
                "description": "Monthly revenue visualization",
                "data": {"revenue": [1000, 2000, 3000]},
                "source_queries": [{"name": "revenue", "sql": "SELECT month, revenue FROM sales"}],
            }
        )

        assert result["status"] == "created"
        assert "artifact_id" in result
        assert result["title"] == "Revenue Chart"
        assert result["type"] == "react"
        assert "/artifacts/" in result["render_url"]
        assert "/render" in result["render_url"]

        # Verify artifact was created in database
        artifact = await Artifact.objects.aget(id=result["artifact_id"])
        assert artifact.title == "Revenue Chart"
        assert artifact.artifact_type == "react"
        assert artifact.code == "export default function Chart() { return <div>Chart</div>; }"
        assert artifact.data["revenue"] == [1000, 2000, 3000]
        assert artifact.source_queries == [
            {"name": "revenue", "sql": "SELECT month, revenue FROM sales"}
        ]
        assert artifact.version == 1
        assert artifact.parent_artifact is None

    @pytest.mark.asyncio
    async def test_update_artifact_tool(self, user, workspace, artifact, tenant_membership):
        """Test update_artifact tool creates a new version of an artifact."""
        from apps.agents.tools.artifact_tool import create_artifact_tools

        tools = create_artifact_tools(workspace, user)
        update_artifact_tool = tools[1]

        original_version = artifact.version
        new_code = "export default function Chart() { return <div>Updated Chart</div>; }"

        result = await update_artifact_tool.ainvoke(
            {
                "artifact_id": str(artifact.id),
                "code": new_code,
                "title": "Updated Chart Title",
                "data": {"rows": [{"x": 2, "y": 4}]},
            }
        )

        assert result["status"] == "updated"
        assert "artifact_id" in result
        assert result["version"] == original_version + 1

        # Update creates a NEW artifact (not in-place), verify the new one
        new_artifact = await Artifact.objects.aget(id=result["artifact_id"])
        assert new_artifact.version == original_version + 1
        assert new_artifact.code == new_code
        assert new_artifact.title == "Updated Chart Title"
        assert new_artifact.data == {"rows": [{"x": 2, "y": 4}]}

    @pytest.mark.asyncio
    async def test_update_creates_new_version(self, user, workspace, artifact, tenant_membership):
        """Test that update_artifact creates new artifacts with incrementing versions."""
        from apps.agents.tools.artifact_tool import create_artifact_tools

        tools = create_artifact_tools(workspace, user)
        update_artifact_tool = tools[1]

        original_version = artifact.version

        # First update - creates new artifact from original
        result1 = await update_artifact_tool.ainvoke(
            {
                "artifact_id": str(artifact.id),
                "code": "export default function Chart() { return <div>Version 2</div>; }",
            }
        )

        assert result1["status"] == "updated"
        assert result1["version"] == original_version + 1

        # Second update - creates new artifact from the v2 artifact
        result2 = await update_artifact_tool.ainvoke(
            {
                "artifact_id": result1["artifact_id"],
                "code": "export default function Chart() { return <div>Version 3</div>; }",
            }
        )

        assert result2["status"] == "updated"
        assert result2["version"] == original_version + 2
