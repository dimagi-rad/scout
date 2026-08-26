"""Tests for shared/public thread artifact rendering (issue #240, finding 00#8).

A shared thread page must surface the artifacts created during that conversation
so the public viewer can render them in a sandbox. Two things have to hold:

1. Artifacts created during a chat carry a non-empty ``conversation_id`` equal to
   the thread id, so the public page filter (``conversation_id=str(thread_id)``)
   actually finds them.
2. The public-thread endpoint returns each artifact's ``code`` and ``data`` so
   the public page can render it in a client-side sandboxed iframe (srcdoc)
   instead of dumping ``<pre>`` source. Live semantic data (authenticated
   query-data / sandbox routes) is intentionally NOT exposed to anonymous
   viewers.
"""

import pytest
from django.contrib.auth import get_user_model
from django.test import AsyncClient

from apps.artifacts.models import Artifact, ArtifactType
from apps.chat.models import Thread
from apps.users.models import Tenant
from apps.workspaces.models import (
    Workspace,
    WorkspaceMembership,
    WorkspaceRole,
    WorkspaceTenant,
)

User = get_user_model()


async def _setup_shared_thread_with_artifact():
    user = await User.objects.acreate_user(email="sharer@example.com", password="pass")
    tenant = await Tenant.objects.acreate(
        provider="commcare", external_id="share-domain", canonical_name="Share"
    )
    ws = await Workspace.objects.acreate(name="Share WS", created_by=user)
    await WorkspaceMembership.objects.acreate(workspace=ws, user=user, role=WorkspaceRole.MANAGE)
    await WorkspaceTenant.objects.acreate(workspace=ws, tenant=tenant)

    thread = Thread(workspace=ws, user=user, title="Shared analysis", is_shared=True)
    await thread.asave()  # save() mints the share_token

    artifact = await Artifact.objects.acreate(
        workspace=ws,
        created_by=user,
        title="Live Dashboard",
        artifact_type=ArtifactType.STORY,
        code="",
        data={"story_doc": {"version": 1, "blocks": []}},
        conversation_id=str(thread.id),
        semantic_queries=[{"name": "q", "measures": ["visits.count"]}],
    )
    # A second artifact from a DIFFERENT conversation must NOT leak in.
    await Artifact.objects.acreate(
        workspace=ws,
        created_by=user,
        title="Other",
        artifact_type=ArtifactType.REACT,
        code="export default function O(){return <div/>}",
        conversation_id="some-other-thread",
    )
    return ws, thread, artifact


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_public_thread_returns_only_its_artifacts():
    """The public-thread endpoint returns artifacts filtered by conversation_id."""
    _ws, thread, artifact = await _setup_shared_thread_with_artifact()

    client = AsyncClient()
    resp = await client.get(f"/api/chat/threads/shared/{thread.share_token}/")

    assert resp.status_code == 200
    body = resp.json()
    artifacts = body["artifacts"]
    assert [a["id"] for a in artifacts] == [str(artifact.id)]


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_public_thread_artifact_returns_code_for_sandbox_render():
    """The returned artifact carries the code + data needed to render it in a
    client-side sandboxed iframe on the public page (not just <pre> source)."""
    _ws, thread, artifact = await _setup_shared_thread_with_artifact()

    client = AsyncClient()
    resp = await client.get(f"/api/chat/threads/shared/{thread.share_token}/")

    assert resp.status_code == 200
    art = resp.json()["artifacts"][0]

    assert art["id"] == str(artifact.id)
    assert art["artifact_type"] == artifact.artifact_type
    assert art["code"] == artifact.code
    # Live/authenticated routes must NOT leak to anonymous viewers.
    assert "render_url" not in art


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_public_thread_with_empty_conversation_id_artifacts_finds_none():
    """If chat-created artifacts had an empty conversation_id (the original bug),
    the public page would find zero artifacts. Guard the filter contract."""
    ws, thread, _artifact = await _setup_shared_thread_with_artifact()

    # thread.user is the cached instance set at construction (no DB hit).
    await Artifact.objects.acreate(
        workspace=ws,
        created_by=thread.user,
        title="Orphan",
        artifact_type=ArtifactType.REACT,
        code="export default function X(){return <div/>}",
        conversation_id="",  # the pre-fix bug value
    )

    client = AsyncClient()
    resp = await client.get(f"/api/chat/threads/shared/{thread.share_token}/")

    assert resp.status_code == 200
    titles = [a["title"] for a in resp.json()["artifacts"]]
    # The orphan (conversation_id="") must not appear; only the correctly-tagged one.
    assert "Orphan" not in titles
    assert "Live Dashboard" in titles
