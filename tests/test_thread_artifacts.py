import json
from uuid import uuid4

import pytest
from asgiref.sync import sync_to_async
from django.contrib.auth.models import update_last_login
from django.contrib.auth.signals import user_logged_in
from django.test import AsyncClient

from apps.artifacts.models import Artifact, ArtifactType
from apps.chat.artifact_links import link_artifact_to_thread
from apps.chat.models import Thread, ThreadArtifact


async def _auth_client(user):
    client = AsyncClient()
    user_logged_in.disconnect(update_last_login)
    try:
        await sync_to_async(client.force_login, thread_sensitive=True)(user)
    finally:
        user_logged_in.connect(update_last_login)
    return client


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_link_artifact_to_thread_creates_explicit_relation(workspace, user):
    thread = await Thread.objects.acreate(
        workspace=workspace,
        user=user,
        title="Artifact work",
    )
    artifact = await Artifact.objects.acreate(
        workspace=workspace,
        created_by=user,
        title="Completion Snapshot",
        artifact_type=ArtifactType.STORY,
        code="",
        conversation_id=str(thread.id),
        data={"story_doc": {"version": 1, "blocks": []}},
    )

    link = await link_artifact_to_thread(
        artifact,
        str(thread.id),
        workspace,
        source=ThreadArtifact.Source.CREATED,
        tool_call_id="toolu_123",
    )

    assert link is not None
    assert link.thread_id == thread.id
    assert link.artifact_id == artifact.id
    assert link.source == ThreadArtifact.Source.CREATED
    assert link.tool_call_id == "toolu_123"


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_thread_artifacts_endpoint_backfills_legacy_conversation_artifacts(workspace, user):
    thread = await Thread.objects.acreate(
        workspace=workspace,
        user=user,
        title="Legacy thread",
    )
    await Artifact.objects.acreate(
        workspace=workspace,
        created_by=user,
        title="Legacy Chart",
        artifact_type=ArtifactType.REACT,
        code="export default function Chart() { return null }",
        conversation_id=str(thread.id),
    )

    client = await _auth_client(user)
    response = await client.get(f"/api/workspaces/{workspace.id}/threads/{thread.id}/artifacts/")

    assert response.status_code == 200
    payload = response.json()
    assert [item["title"] for item in payload["results"]] == ["Legacy Chart"]
    assert payload["results"][0]["source"] == "created"
    assert await ThreadArtifact.objects.filter(thread=thread).acount() == 1


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_thread_artifacts_endpoint_backfills_saved_tool_artifact_references(
    monkeypatch,
    workspace,
    user,
):
    thread = await Thread.objects.acreate(
        workspace=workspace,
        user=user,
        title="Saved artifact reference",
    )
    artifact = await Artifact.objects.acreate(
        workspace=workspace,
        created_by=user,
        title="Saved Tool Artifact",
        artifact_type=ArtifactType.STORY,
        code="",
        conversation_id="",
        data={"story_doc": {"version": 1, "blocks": []}},
    )

    async def fake_load_thread_ui_messages(thread_id):
        assert thread_id == str(thread.id)
        return [
            {
                "role": "assistant",
                "parts": [
                    {
                        "type": "tool-artifact_manager",
                        "output": json.dumps(
                            {
                                "status": "created",
                                "artifact": {"id": str(artifact.id)},
                            }
                        ),
                    }
                ],
            }
        ]

    monkeypatch.setattr(
        "apps.chat.artifact_links._load_thread_ui_messages",
        fake_load_thread_ui_messages,
    )

    client = await _auth_client(user)
    response = await client.get(f"/api/workspaces/{workspace.id}/threads/{thread.id}/artifacts/")

    assert response.status_code == 200
    payload = response.json()
    assert [item["title"] for item in payload["results"]] == ["Saved Tool Artifact"]
    assert payload["results"][0]["source"] == "created"
    assert await ThreadArtifact.objects.filter(thread=thread, artifact=artifact).acount() == 1


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_thread_artifacts_endpoint_collapses_artifact_versions(workspace, user):
    thread = await Thread.objects.acreate(
        workspace=workspace,
        user=user,
        title="Artifact versions",
    )
    artifact_v1 = await Artifact.objects.acreate(
        workspace=workspace,
        created_by=user,
        title="Verified visits",
        artifact_type=ArtifactType.STORY,
        code="",
        conversation_id=str(thread.id),
        data={"story_doc": {"version": 1, "blocks": [{"id": "a", "type": "markdown"}]}},
    )
    artifact_v2 = await sync_to_async(artifact_v1.create_new_version, thread_sensitive=True)(
        data={"story_doc": {"version": 1, "blocks": [{"id": "b", "type": "markdown"}]}},
    )
    await link_artifact_to_thread(
        artifact_v1,
        str(thread.id),
        workspace,
        source=ThreadArtifact.Source.CREATED,
    )
    await link_artifact_to_thread(
        artifact_v2,
        str(thread.id),
        workspace,
        source=ThreadArtifact.Source.UPDATED,
    )

    client = await _auth_client(user)
    response = await client.get(f"/api/workspaces/{workspace.id}/threads/{thread.id}/artifacts/")

    assert response.status_code == 200
    payload = response.json()
    assert [(item["id"], item["version"], item["source"]) for item in payload["results"]] == [
        (str(artifact_v2.id), 2, "updated")
    ]


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_thread_title_patch_creates_untitled_thread_row(workspace, user):
    thread_id = uuid4()
    client = await _auth_client(user)

    response = await client.patch(
        f"/api/workspaces/{workspace.id}/threads/{thread_id}/",
        data=json.dumps({"title": "UX test artifact creation"}),
        content_type="application/json",
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["id"] == str(thread_id)
    assert payload["title"] == "UX test artifact creation"
    thread = await Thread.objects.aget(id=thread_id)
    assert thread.workspace_id == workspace.id
    assert thread.user_id == user.id
    assert thread.title_is_custom is True


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_thread_detail_hides_legacy_auto_title_until_user_renames(workspace, user):
    thread = await Thread.objects.acreate(
        workspace=workspace,
        user=user,
        title="What are module completion rates?",
        title_is_custom=False,
    )
    client = await _auth_client(user)

    response = await client.get(f"/api/workspaces/{workspace.id}/threads/{thread.id}/")

    assert response.status_code == 200
    payload = response.json()
    assert payload["title"] == "Untitled"
    assert payload["history_title"] == "What are module completion rates?"
    assert payload["title_is_custom"] is False


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_thread_list_returns_history_title_for_non_custom_threads(workspace, user):
    thread = await Thread.objects.acreate(
        workspace=workspace,
        user=user,
        title="What are module completion rates?",
        title_is_custom=False,
    )
    client = await _auth_client(user)

    response = await client.get(f"/api/workspaces/{workspace.id}/threads/")

    assert response.status_code == 200
    payload = response.json()
    assert payload[0]["id"] == str(thread.id)
    assert payload[0]["title"] == "Untitled"
    assert payload[0]["history_title"] == "What are module completion rates?"
    assert payload[0]["title_is_custom"] is False


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_thread_list_uses_first_user_message_when_history_title_is_blank(
    monkeypatch,
    workspace,
    user,
):
    thread = await Thread.objects.acreate(
        workspace=workspace,
        user=user,
        title="",
        title_is_custom=False,
    )

    async def fake_load_thread_messages(thread_id):
        assert thread_id == thread.id
        return [
            {
                "role": "user",
                "parts": [
                    {
                        "type": "text",
                        "text": "Build an artifact from example queries",
                    }
                ],
            }
        ]

    monkeypatch.setattr(
        "apps.chat.thread_views._load_thread_messages",
        fake_load_thread_messages,
    )

    client = await _auth_client(user)
    response = await client.get(f"/api/workspaces/{workspace.id}/threads/")

    assert response.status_code == 200
    payload = response.json()
    assert payload[0]["id"] == str(thread.id)
    assert payload[0]["title"] == "Untitled"
    assert payload[0]["history_title"] == "Build an artifact from example queries"


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_thread_title_patch_shortens_long_names(workspace, user):
    thread_id = uuid4()
    client = await _auth_client(user)
    long_title = f"{'a' * 205} tail"

    response = await client.patch(
        f"/api/workspaces/{workspace.id}/threads/{thread_id}/",
        data=json.dumps({"title": long_title}),
        content_type="application/json",
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["title"] == f"{'a' * 200}..."
    assert payload["title_is_custom"] is True
    thread = await Thread.objects.aget(id=thread_id)
    assert thread.title == f"{'a' * 200}..."
