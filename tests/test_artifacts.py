"""
Comprehensive tests for Phase 3 (Frontend & Artifacts) of the Scout data agent platform.

Tests artifact models, views, access control, versioning, and artifact tools.
"""

import uuid

import pytest
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from django.test import AsyncClient, Client

from apps.agents.tools.artifact_tool import create_artifact_tools
from apps.artifacts.models import Artifact, ArtifactType
from apps.users.models import Tenant
from apps.workspaces.models import (
    Workspace,
    WorkspaceMembership,
    WorkspaceRole,
    WorkspaceTenant,
)

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

    def test_sandbox_returns_html(self, authenticated_client, artifact, workspace):
        """Test that sandbox view returns HTML content."""
        response = authenticated_client.get(
            f"/api/workspaces/{workspace.id}/artifacts/{artifact.id}/sandbox/"
        )

        assert response.status_code == 200
        assert "text/html" in response["Content-Type"]

        # Check for key sandbox elements
        content = response.content.decode()
        assert "<!DOCTYPE html>" in content
        assert "Artifact Sandbox" in content
        assert "React" in content or "react" in content
        assert "root" in content

    def test_sandbox_supports_print_to_pdf(self, authenticated_client, artifact, workspace):
        """Sandbox HTML wires up print-to-PDF: print CSS and a scout-print listener."""
        response = authenticated_client.get(
            f"/api/workspaces/{workspace.id}/artifacts/{artifact.id}/sandbox/"
        )

        assert response.status_code == 200
        content = response.content.decode()

        # Print-optimized styling is present.
        assert "@media print" in content
        # Parent frame triggers print via a postMessage handler.
        assert "scout-print" in content
        assert "window.print()" in content

    def test_scout_print_receiver_validates_source_not_origin(
        self, authenticated_client, artifact, workspace
    ):
        """The scout-print receiver must gate on event.source, not event.origin.

        The iframe is sandboxed WITHOUT allow-same-origin, so its document has an
        opaque ("null") security origin. The parent posts scout-print from the
        app's concrete origin, so an `event.origin === window.location.origin`
        guard would reject the legitimate message (event.origin != "null") AND
        would trust forgeries from other "null"-origin sandboxed frames. The
        receiver must instead accept only messages whose source is the parent
        window (mirroring ArtifactPanel's source-based check).
        """
        response = authenticated_client.get(
            f"/api/workspaces/{workspace.id}/artifacts/{artifact.id}/sandbox/"
        )

        assert response.status_code == 200
        content = response.content.decode()

        # Isolate the scout-print message listener block.
        anchor = content.index("Print-to-PDF")
        listener_start = content.index("window.addEventListener('message'", anchor)
        listener_end = content.index("});", listener_start)
        listener = content[listener_start:listener_end]

        # The scout-print receiver gates on the message source (trusted parent),
        assert "event.source !== window.parent" in listener
        # and does NOT gate it on origin equality (which breaks opaque-origin frames).
        assert "event.origin !== window.location.origin" not in listener

    def test_iframe_to_parent_messages_use_wildcard_target_origin(
        self, authenticated_client, artifact, workspace
    ):
        """iframe->parent postMessage must target "*", never window.location.origin.

        The iframe is sandboxed WITHOUT allow-same-origin, so its document has an
        opaque ("null") security origin: inside the frame
        window.location.origin === "null". A postMessage whose targetOrigin is a
        concrete origin string (or "null") will NOT match the parent's real
        concrete origin, so the browser SILENTLY DROPS the message. Both
        iframe->parent sends (artifact-query-data and artifact-error) must use
        targetOrigin "*". This is safe because the parent (ArtifactPanel)
        authenticates inbound messages by event.source === the iframe's
        contentWindow, not by origin.
        """
        response = authenticated_client.get(
            f"/api/workspaces/{workspace.id}/artifacts/{artifact.id}/sandbox/"
        )

        assert response.status_code == 200
        content = response.content.decode()

        # No iframe->parent postMessage call may target the document origin
        # (window.location.origin == "null"). Match the concrete call form
        # `}, window.location.origin)` so explanatory comments don't trip it.
        assert "}, window.location.origin)" not in content, (
            "iframe->parent postMessage still targets window.location.origin, "
            "which is 'null' for an opaque-origin sandbox frame and will be "
            "silently dropped by the browser."
        )

        # Both message types must be posted to the parent with the "*" target.
        # The targetOrigin is the final argument on the `}, <target>);` line that
        # closes each postMessage call; locate it from the message type marker.
        for msg_type in ("artifact-query-data", "artifact-error"):
            idx = content.index(f"type: '{msg_type}'")
            close = content.index("}, ", idx)
            # Slice the closing line up to the call terminator `);`.
            target_arg = content[close + len("}, ") : content.index(");", close)]
            assert target_arg == "'*'", (
                f"iframe->parent '{msg_type}' postMessage must use targetOrigin "
                f"'*'; found: {target_arg!r}"
            )

    def test_sandbox_live_query_fetch_respects_script_prefix(
        self, authenticated_client, artifact, workspace
    ):
        """The in-iframe live-query fetch must honor FORCE_SCRIPT_NAME (issue #248, 04#8b).

        On the labs deployment Scout is mounted under /scout (FORCE_SCRIPT_NAME),
        and nginx only proxies /scout/api/... A root-relative '/api/...' fetch
        from the sandbox HTML would hit the host root and 404. The sandbox must
        prefix the request with the request's SCRIPT_NAME.
        """
        response = authenticated_client.get(
            f"/api/workspaces/{workspace.id}/artifacts/{artifact.id}/sandbox/",
            SCRIPT_NAME="/scout",
        )

        assert response.status_code == 200
        content = response.content.decode()

        # The injected base must be the request's script prefix...
        assert 'const API_BASE = "/scout";' in content
        # ...and the live-query fetch must be built from it (not a bare
        # leading-slash path that bypasses the mount point).
        assert "fetch(API_BASE + '/api/workspaces/'" in content

    def test_sandbox_live_query_fetch_at_root_mount(
        self, authenticated_client, artifact, workspace
    ):
        """With no script prefix the fetch URL stays root-relative (no double slash)."""
        response = authenticated_client.get(
            f"/api/workspaces/{workspace.id}/artifacts/{artifact.id}/sandbox/"
        )

        assert response.status_code == 200
        content = response.content.decode()
        # Empty base → fetch resolves to a clean root-relative URL at runtime.
        assert 'const API_BASE = "";' in content
        assert "fetch(API_BASE + '/api/workspaces/'" in content

    def test_sandbox_csp_headers(self, authenticated_client, artifact, workspace):
        """Test that CSP headers are set correctly for security."""
        response = authenticated_client.get(
            f"/api/workspaces/{workspace.id}/artifacts/{artifact.id}/sandbox/"
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

    def test_get_artifact_data_authenticated(self, authenticated_client, artifact, workspace):
        """Test authenticated user with workspace access can get artifact data."""
        response = authenticated_client.get(
            f"/api/workspaces/{workspace.id}/artifacts/{artifact.id}/data/"
        )

        assert response.status_code == 200
        data = response.json()

        assert data["id"] == str(artifact.id)
        assert data["title"] == artifact.title
        assert data["type"] == artifact.artifact_type
        assert data["code"] == artifact.code
        assert data["data"] == artifact.data
        assert data["version"] == artifact.version

    def test_get_artifact_data_unauthenticated(self, client, artifact, workspace):
        """Test unauthenticated user cannot access artifact data."""
        response = client.get(f"/api/workspaces/{workspace.id}/artifacts/{artifact.id}/data/")

        assert response.status_code == 401
        data = response.json()
        assert "error" in data

    def test_get_artifact_data_not_found(self, authenticated_client, workspace):
        """Test accessing non-existent artifact returns 404."""
        fake_id = uuid.uuid4()
        response = authenticated_client.get(
            f"/api/workspaces/{workspace.id}/artifacts/{fake_id}/data/"
        )

        assert response.status_code == 404

    def test_artifact_data_requires_workspace_membership(self, db, user, client):
        """Test that artifact access requires workspace membership (no membership -> 403)."""
        from apps.users.models import Tenant, TenantMembership
        from apps.workspaces.models import (
            Workspace,
            WorkspaceMembership,
            WorkspaceRole,
            WorkspaceTenant,
        )

        # Create a workspace owned by a different user (no membership for `user`)
        other_user = User.objects.create_user(email="other2@example.com", password="pass")
        other_tenant = Tenant.objects.create(
            provider="commcare", external_id="other-domain", canonical_name="Other Domain"
        )
        other_workspace = Workspace.objects.create(name="Other Domain", created_by=other_user)
        WorkspaceTenant.objects.create(workspace=other_workspace, tenant=other_tenant)
        TenantMembership.objects.create(user=other_user, tenant=other_tenant)
        WorkspaceMembership.objects.create(
            workspace=other_workspace, user=other_user, role=WorkspaceRole.MANAGE
        )
        other_artifact = Artifact.objects.create(
            workspace=other_workspace,
            created_by=other_user,
            title="Other Artifact",
            artifact_type=ArtifactType.HTML,
            code="<div>Other</div>",
            version=1,
            conversation_id="conv_other",
        )

        # `user` tries to access artifact in other_workspace -> 403
        client.force_login(user)
        response = client.get(
            f"/api/workspaces/{other_workspace.id}/artifacts/{other_artifact.id}/data/"
        )

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
            }
        )

        assert result["status"] == "created"
        assert "artifact_id" in result
        assert result["title"] == "Revenue Chart"
        assert result["type"] == "react"
        # render_url must point at the real sandbox route, not the dead
        # /artifacts/<id>/render/ path (issue #240, finding 00#8).
        assert result["render_url"] == (
            f"/api/workspaces/{workspace.id}/artifacts/{result['artifact_id']}/sandbox/"
        )

        # Verify artifact was created in database
        artifact = await Artifact.objects.aget(id=result["artifact_id"])
        assert artifact.title == "Revenue Chart"
        assert artifact.artifact_type == "react"
        assert artifact.code == "export default function Chart() { return <div>Chart</div>; }"
        assert artifact.data["revenue"] == [1000, 2000, 3000]
        assert artifact.source_queries == []
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

    @pytest.mark.asyncio
    async def test_create_artifact_threads_conversation_id(self, user, workspace):
        """create_artifact must persist the conversation_id it was built with.

        Regression for issue #240, finding 00#8: chat-created artifacts stored
        conversation_id='' because _build_tools never passed it, so shared/public
        thread pages (which filter conversation_id=str(thread_id)) showed ZERO
        artifacts.
        """
        tools = create_artifact_tools(workspace, user, conversation_id="thread-xyz")
        create_artifact_tool = tools[0]

        result = await create_artifact_tool.ainvoke(
            {
                "title": "Threaded Chart",
                "artifact_type": "react",
                "code": "export default function C() { return <div/>; }",
            }
        )

        assert result["status"] == "created"
        artifact = await Artifact.objects.aget(id=result["artifact_id"])
        assert artifact.conversation_id == "thread-xyz"

    @pytest.mark.asyncio
    async def test_update_artifact_render_url_points_at_sandbox(
        self, user, workspace, artifact, tenant_membership
    ):
        """update_artifact's render_url must resolve to the real sandbox route."""
        tools = create_artifact_tools(workspace, user)
        update_artifact_tool = tools[1]

        result = await update_artifact_tool.ainvoke(
            {
                "artifact_id": str(artifact.id),
                "code": "export default function C() { return <div>v2</div>; }",
            }
        )

        assert result["status"] == "updated"
        assert result["render_url"] == (
            f"/api/workspaces/{workspace.id}/artifacts/{result['artifact_id']}/sandbox/"
        )


@pytest.mark.django_db(transaction=True)
class TestArtifactQueryDataRouting:
    """Tests for ArtifactQueryDataView schema routing (issue #240, finding 00#6)."""

    @pytest.mark.asyncio
    async def test_legacy_source_queries_do_not_execute(self, user):
        """Legacy SQL-backed source_queries return a disabled error."""
        ws = await Workspace.objects.acreate(name="Multi WS", created_by=user)
        await WorkspaceMembership.objects.acreate(
            workspace=ws, user=user, role=WorkspaceRole.MANAGE
        )
        for ext in ("first-tenant", "second-tenant"):
            t = await Tenant.objects.acreate(
                provider="commcare", external_id=ext, canonical_name=ext
            )
            await WorkspaceTenant.objects.acreate(workspace=ws, tenant=t)
        art = await Artifact.objects.acreate(
            workspace=ws,
            created_by=user,
            title="Live",
            artifact_type=ArtifactType.REACT,
            code="export default function C(){return <div/>}",
            source_queries=[{"name": "q", "sql": "SELECT 1"}],
        )

        client = AsyncClient()
        await sync_to_async(client.force_login)(user)

        resp = await client.get(f"/api/workspaces/{ws.id}/artifacts/{art.id}/query-data/")

        assert resp.status_code == 200
        body = resp.json()
        assert body["queries"] == [
            {
                "name": "q",
                "error": (
                    "Legacy SQL-backed artifact queries are disabled. "
                    "Recreate this artifact with semantic_queries."
                ),
            }
        ]
