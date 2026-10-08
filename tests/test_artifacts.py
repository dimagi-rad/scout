"""
Comprehensive tests for Phase 3 (Frontend & Artifacts) of the Scout data agent platform.

Tests artifact models, views, access control and versioning.
"""

import json
import re
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from django.test import AsyncClient, Client

from apps.artifacts.models import Artifact, ArtifactType
from apps.artifacts.services.export import ArtifactExporter
from apps.artifacts.views import SANDBOX_FLAGS, SANDBOX_HTML_TEMPLATE
from apps.telemetry.models import EventKind, TelemetryEvent
from apps.users.models import Tenant, TenantMembership
from apps.workspaces.models import (
    Workspace,
    WorkspaceMembership,
    WorkspaceRole,
    WorkspaceTenant,
)
from tests.tenant_access import ausable_connection, usable_connection

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
            ArtifactType.SVG,
            ArtifactType.STORY,
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
        assert "plotly" not in artifact_types
        assert "svg" in artifact_types
        assert "story" in artifact_types


# ============================================================================
# 3. TestArtifactListView
# ============================================================================


@pytest.mark.django_db
class TestArtifactListView:
    def test_list_hides_unsupported_legacy_artifacts(
        self,
        authenticated_client,
        artifact,
        user,
        workspace,
    ):
        Artifact.objects.create(
            workspace=workspace,
            created_by=user,
            title="Legacy Plotly chart",
            artifact_type="plotly",
            code='{"data": []}',
            conversation_id="legacy-thread",
        )

        response = authenticated_client.get(f"/api/workspaces/{workspace.id}/artifacts/")

        assert response.status_code == 200
        assert [item["id"] for item in response.json()["results"]] == [str(artifact.id)]


def _run_sandbox_error_listeners(dispatch: str) -> list:
    """Run the template's real error helpers and window listeners under node.

    `dispatch` calls `handlers.error(...)` / `handlers.unhandledrejection(...)`, or the
    helpers directly, pushing to `calls`. Returns the notifyParentOfError calls, and any
    pushed rows, with any stack replaced by the string "stack".
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    start = SANDBOX_HTML_TEMPLATE.index("function nonErrorMessage(")
    last = SANDBOX_HTML_TEMPLATE.index("window.addEventListener('unhandledrejection'")
    end = SANDBOX_HTML_TEMPLATE.index("\n        });", last) + len("\n        });")
    harness = (
        "const handlers = {}; const calls = [];\n"
        "const window = { addEventListener: (type, fn) => { handlers[type] = fn } };\n"
        "const ArtifactRenderer = { notifyParentOfError: (...args) => calls.push(args) };\n"
        f"{SANDBOX_HTML_TEMPLATE[start:end]}\n"
        f"{dispatch}"
        "const stackless = calls.map(([t, m, s, n]) => [t, m, s && 'stack', n]);\n"
        "console.log(JSON.stringify(stackless));\n"
    )
    result = subprocess.run(  # noqa: S603 - node from PATH runs a fixed harness
        [node, "-e", harness], capture_output=True, text=True, check=False, timeout=30
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


# ============================================================================
# 4. TestArtifactSandboxView
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
        assert "Recharts" in content
        assert "Plotly" not in content
        assert "root" in content

    def test_sandbox_records_an_artifact_view(self, authenticated_client, artifact, workspace):
        authenticated_client.get(f"/api/workspaces/{workspace.id}/artifacts/{artifact.id}/sandbox/")

        view = TelemetryEvent.objects.get(kind=EventKind.ARTIFACT_VIEW)
        assert view.workspace_id == workspace.id
        assert view.name == artifact.artifact_type
        assert view.attrs["artifact_id"] == str(artifact.id)

    def test_sandbox_has_no_story_renderer(self):
        # Stories render through the React ArtifactGraph and never load this
        # iframe; its old block renderer drifted from the story_doc schema.
        assert "renderStory" not in SANDBOX_HTML_TEMPLATE
        assert "case 'story'" not in SANDBOX_HTML_TEMPLATE

    def test_react_export_loads_recharts_without_plotly(self, artifact):
        content = ArtifactExporter(artifact).export_html()

        assert "Recharts.js" in content
        assert "plotly" not in content.lower()

    def test_export_rejects_removed_artifact_types(self, artifact):
        artifact.artifact_type = "plotly"

        with pytest.raises(ValueError, match="Unsupported artifact type: plotly"):
            ArtifactExporter(artifact).export_html()

    def test_export_rejects_story_until_a_standalone_renderer_exists(self, artifact):
        artifact.artifact_type = ArtifactType.STORY

        with pytest.raises(ValueError, match="Unsupported artifact type: story"):
            ArtifactExporter(artifact).export_html()

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
        concrete origin, so the browser SILENTLY DROPS the message. The
        iframe->parent artifact-error send must use targetOrigin "*". This is
        safe because the parent (ArtifactCanvas) authenticates inbound messages
        by event.source === the iframe's contentWindow, not by origin.
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

        # The targetOrigin is the final argument on the `}, <target>);` line that
        # closes the postMessage call; locate it from the message type marker.
        idx = content.index("type: 'artifact-error'")
        close = content.index("}, ", idx)
        target_arg = content[close + len("}, ") : content.index(");", close)]
        assert target_arg == "'*'", (
            f"iframe->parent 'artifact-error' postMessage must use targetOrigin "
            f"'*'; found: {target_arg!r}"
        )

    def test_sandbox_reports_errors_the_renderer_does_not_see(
        self, authenticated_client, artifact, workspace
    ):
        """React render crashes and async errors must reach the parent for Sentry.

        The in-frame React boundary renders the error inline without calling
        showError, and errors thrown from handlers or timers bypass both.
        """
        response = authenticated_client.get(
            f"/api/workspaces/{workspace.id}/artifacts/{artifact.id}/sandbox/"
        )
        content = response.content.decode()

        did_catch = content[content.index("componentDidCatch(error)") :]
        assert did_catch.index("notifyParentOfError(") < did_catch.index("render()")
        assert "window.addEventListener('error'" in content
        assert "window.addEventListener('unhandledrejection'" in content
        assert "error: { title, message, details, name }" in content

    def test_sandbox_reports_non_error_rejections_without_their_value(self):
        """`Promise.reject("query timed out")` must reach Sentry, but a rejected row must not."""
        calls = _run_sandbox_error_listeners(
            "const reasons = ['query timed out', { rows: [{ patient: 'Alice' }] }, 42, null,"
            " new TypeError('bad')];\n"
            "for (const reason of reasons) handlers.unhandledrejection({ reason });\n"
        )

        assert calls == [
            ["Unhandled Rejection", "query timed out", None, "UnhandledRejection"],
            ["Unhandled Rejection", "Non-Error rejection (object)", None, "UnhandledRejection"],
            ["Unhandled Rejection", "Non-Error rejection (number)", None, "UnhandledRejection"],
            ["Unhandled Rejection", "Non-Error rejection (null)", None, "UnhandledRejection"],
            ["Unhandled Rejection", "bad", "stack", "TypeError"],
        ]

    def test_sandbox_reads_no_fields_of_a_thrown_non_error(self):
        """`throw row` must forward neither the row's fields nor its stringified value."""
        calls = _run_sandbox_error_listeners(
            "handlers.error({ error: { message: 'Alice', name: 'Bob' },"
            " message: 'Uncaught [object Object]' });\n"
            "handlers.error({ error: ['Alice', 'Bob'], message: 'Uncaught Alice,Bob' });\n"
            "handlers.error({ error: 'query timed out', message: 'Uncaught query timed out' });\n"
            "handlers.error({ error: null, message: 'Script error.' });\n"
            "handlers.error({ error: new RangeError('bad'), message: 'Uncaught RangeError: bad' });\n"
        )

        assert calls == [
            ["Uncaught Error", "Non-Error exception (object)", None, None],
            ["Uncaught Error", "Non-Error exception (object)", None, None],
            ["Uncaught Error", "query timed out", None, None],
            ["Uncaught Error", "Script error.", None, None],
            ["Uncaught Error", "bad", "stack", "RangeError"],
        ]

    def test_sandbox_catch_sites_read_no_fields_of_a_thrown_non_error(self):
        """A render-time `throw row` or `throw null` goes through describeThrown."""
        fields = re.compile(r"\berror\.(message|stack|name)\b")
        # Each body runs to its closing brace, which sits at the `catch`'s own indent.
        catch_bodies = re.findall(
            r"\n( *)\} catch \(error\) \{(.*?)\n\1\}", SANDBOX_HTML_TEMPLATE, flags=re.DOTALL
        )
        reads_fields = [body.strip() for _, body in catch_bodies if fields.search(body)]
        # JSON.parse of the page's own embedded data only ever throws a SyntaxError.
        assert len(reads_fields) == 1
        assert reads_fields[0].startswith("this.showError('Parse Error'")
        boundary_start = SANDBOX_HTML_TEMPLATE.index("class _ErrorBoundary")
        boundary = SANDBOX_HTML_TEMPLATE[
            boundary_start : SANDBOX_HTML_TEMPLATE.index("render() {", boundary_start)
        ]
        assert not fields.search(boundary)

        calls = _run_sandbox_error_listeners(
            "for (const thrown of [null, { message: 'Alice', name: 'Bob' }, 'boom',"
            " new TypeError('bad')]) {\n"
            "  const d = describeThrown(thrown);\n"
            "  calls.push(['describe', d.message, d.stack, d.name]);\n"
            "}\n"
        )

        assert calls == [
            ["describe", "Non-Error exception (null)", None, None],
            ["describe", "Non-Error exception (object)", None, None],
            ["describe", "boom", None, None],
            ["describe", "bad", "stack", "TypeError"],
        ]

    def test_sandbox_never_fetches_live_data_itself(
        self, authenticated_client, artifact, workspace
    ):
        """The opaque-origin sandbox can't make credentialed API calls (#284, #376).

        Its fetch of /query-data/ was always CORS-blocked, so a live-query
        artifact only ever showed "Data Fetch Error". CORS for the null origin
        must not be added: it would give agent code a path back into the API.
        """
        artifact.semantic_queries = [{"name": "q", "query": {}}]
        artifact.save(update_fields=["semantic_queries"])

        response = authenticated_client.get(
            f"/api/workspaces/{workspace.id}/artifacts/{artifact.id}/sandbox/"
        )

        assert response.status_code == 200
        content = response.content.decode()
        assert "query-data" not in content
        assert "has_live_queries" not in content
        # Defence in depth: the frame's CSP doesn't allow the app's own origin.
        connect_src = next(
            d.strip()
            for d in response["Content-Security-Policy"].split(";")
            if d.strip().startswith("connect-src")
        )
        assert connect_src == "connect-src https://cdn.jsdelivr.net"

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
        assert "connect-src https://cdn.jsdelivr.net;" in csp
        assert "img-src data: blob:" in csp

    def test_sandbox_csp_isolates_direct_navigation(
        self, authenticated_client, artifact, workspace
    ):
        """The sandbox is opaque-origin by its own CSP, not only via the iframe attribute."""
        response = authenticated_client.get(
            f"/api/workspaces/{workspace.id}/artifacts/{artifact.id}/sandbox/"
        )

        directives = {
            name: value
            for name, _, value in (
                d.strip().partition(" ") for d in response["Content-Security-Policy"].split(";")
            )
            if name
        }
        assert directives["sandbox"] == "allow-scripts allow-modals"
        assert "allow-same-origin" not in response["Content-Security-Policy"]
        assert directives["frame-ancestors"] == "'self'"
        assert response["X-Frame-Options"] == "SAMEORIGIN"

    def test_sandbox_flags_match_iframe_attribute(self):
        """The CSP flags and the in-app iframe's sandbox attribute must not drift."""
        canvas_path = (
            Path(__file__).resolve().parent.parent
            / "frontend/src/components/ArtifactViewer/ArtifactCanvas.tsx"
        )
        if not canvas_path.exists():
            pytest.fail(f"{canvas_path} moved; point this test at the artifact sandbox iframe")
        assert f'sandbox="{SANDBOX_FLAGS}"' in canvas_path.read_text(), (
            "ArtifactCanvas iframe sandbox attribute drifted from SANDBOX_FLAGS"
        )


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

        # Create a workspace owned by a different user (no membership for `user`)
        other_user = User.objects.create_user(email="other2@example.com", password="pass")
        other_tenant = Tenant.objects.create(
            provider="commcare", external_id="other-domain", canonical_name="Other Domain"
        )
        other_workspace = Workspace.objects.create(name="Other Domain", created_by=other_user)
        WorkspaceTenant.objects.create(workspace=other_workspace, tenant=other_tenant)
        TenantMembership.objects.create(
            user=other_user,
            tenant=other_tenant,
            connection=usable_connection(other_user, other_tenant.provider),
        )
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
            # the all-of access gate needs a usable credential for every tenant
            await TenantMembership.objects.acreate(
                user=user, tenant=t, connection=await ausable_connection(user, t.provider)
            )
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
