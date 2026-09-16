import io
import json
import uuid
import zipfile
from unittest.mock import AsyncMock

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, RequestFactory
from django.utils import timezone
from rest_framework.test import APIRequestFactory

from apps.artifacts.models import Artifact, ArtifactType
from apps.chat.models import Thread, ThreadJob
from apps.knowledge.models import KnowledgeEntry
from apps.recipes.models import Recipe, RecipeRun
from apps.recipes.tasks import run_recipe
from apps.users.models import TenantMembership
from apps.workspaces.models import (
    MaterializationRun,
    TenantSchema,
    WorkspaceDataRecovery,
    WorkspaceMembership,
    WorkspaceRole,
)
from apps.workspaces.tasks import materialize_workspace, recover_workspace_data
from apps.workspaces.workspace_resolver import (
    aresolve_workspace,
    resolve_workspace,
    resolve_workspace_drf,
)

GENERIC_DENIAL = {"error": "Workspace not found or access denied."}


@pytest.mark.django_db
def test_drf_adapter_denies_insufficient_role(read_user, workspace):
    request = APIRequestFactory().get("/")
    request.user = read_user

    resolved, membership, response = resolve_workspace_drf(
        request, workspace.id, minimum_role=WorkspaceRole.READ_WRITE
    )

    assert resolved is None
    assert membership is None
    assert response.status_code == 403
    assert response.data == GENERIC_DENIAL


@pytest.mark.django_db
def test_sync_json_adapter_denies_insufficient_role(read_user, workspace):
    request = RequestFactory().get("/")
    request.user = read_user

    resolved, response = resolve_workspace(
        request.user, workspace.id, minimum_role=WorkspaceRole.READ_WRITE
    )

    assert resolved is None
    assert response.status_code == 403
    assert json.loads(response.content) == GENERIC_DENIAL


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_async_json_adapter_denies_insufficient_role(read_user, workspace):
    resolved, response = await aresolve_workspace(
        read_user, workspace.id, minimum_role=WorkspaceRole.READ_WRITE
    )

    assert resolved is None
    assert response.status_code == 403
    assert json.loads(response.content) == GENERIC_DENIAL


READ_WRITE_MUTATIONS = [
    ("post", "/artifacts/{target}/recovery/", {}),
    ("patch", "/artifacts/{target}/", {"title": "blocked"}),
    ("delete", "/artifacts/{target}/", None),
    ("post", "/artifacts/{target}/undelete/", {}),
    ("post", "/knowledge/", {"type": "entry", "title": "blocked", "content": "x"}),
    ("put", "/knowledge/{target}/", {"title": "blocked"}),
    ("delete", "/knowledge/{target}/", None),
    ("post", "/knowledge/import/", {}),
    ("put", "/recipes/{target}/", {"name": "blocked"}),
    ("delete", "/recipes/{target}/", None),
    ("patch", "/recipes/{target}/runs/{other}/", {"is_public": True}),
    ("post", "/materialization/cancel/", {}),
    ("post", "/materialize/retry/", {}),
    ("post", "/jobs/{target}/cancel/", {}),
    ("patch", "/threads/{target}/share/", {"is_shared": True}),
]


@pytest.mark.django_db
@pytest.mark.parametrize(("method", "suffix", "payload"), READ_WRITE_MUTATIONS)
def test_read_member_cannot_reach_workspace_mutations(
    read_user, workspace, method, suffix, payload
):
    client = Client(enforce_csrf_checks=False)
    client.force_login(read_user)
    url = f"/api/workspaces/{workspace.id}" + suffix.format(target=uuid.uuid4(), other=uuid.uuid4())
    if suffix == "/knowledge/import/":
        response = client.post(url, {})
    else:
        body = b"" if payload is None else json.dumps(payload).encode()
        response = client.generic(method.upper(), url, body, content_type="application/json")

    assert response.status_code == 403
    assert response.json() == GENERIC_DENIAL


@pytest.mark.django_db
@pytest.mark.parametrize(
    "suffix",
    [
        "/artifacts/",
        "/knowledge/",
        "/recipes/",
        "/threads/",
        "/jobs/active/",
    ],
)
def test_read_member_keeps_workspace_get_access(read_user, workspace, suffix):
    client = Client(enforce_csrf_checks=False)
    client.force_login(read_user)

    response = client.get(f"/api/workspaces/{workspace.id}{suffix}")

    assert response.status_code == 200


@pytest.mark.django_db
def test_read_write_member_can_update_shared_content(write_user, workspace):
    artifact = Artifact.objects.create(
        workspace=workspace,
        created_by=write_user,
        title="Before",
        artifact_type=ArtifactType.REACT,
        code="export default function A() {}",
        conversation_id="role-policy",
    )
    recipe = Recipe.objects.create(
        workspace=workspace,
        created_by=write_user,
        name="Before",
        prompt="Run",
    )
    thread = Thread.objects.create(workspace=workspace, user=write_user, title="Share")
    client = Client(enforce_csrf_checks=False)
    client.force_login(write_user)

    artifact_response = client.patch(
        f"/api/workspaces/{workspace.id}/artifacts/{artifact.id}/",
        data=json.dumps({"title": "After"}),
        content_type="application/json",
    )
    knowledge_response = client.post(
        f"/api/workspaces/{workspace.id}/knowledge/",
        data=json.dumps({"type": "entry", "title": "Metric", "content": "Definition"}),
        content_type="application/json",
    )
    recipe_response = client.put(
        f"/api/workspaces/{workspace.id}/recipes/{recipe.id}/",
        data=json.dumps({"name": "After"}),
        content_type="application/json",
    )
    share_response = client.patch(
        f"/api/workspaces/{workspace.id}/threads/{thread.id}/share/",
        data=json.dumps({"is_shared": True}),
        content_type="application/json",
    )

    assert artifact_response.status_code == 200
    artifact.refresh_from_db()
    assert artifact.title == "After"
    assert knowledge_response.status_code == 201
    assert KnowledgeEntry.objects.filter(workspace=workspace, title="Metric").exists()
    assert recipe_response.status_code == 200
    recipe.refresh_from_db()
    assert recipe.name == "After"
    assert share_response.status_code == 200
    thread.refresh_from_db()
    assert thread.is_shared


@pytest.mark.django_db
def test_thread_owner_can_disable_share_after_role_downgrade(write_user, workspace):
    thread = Thread.objects.create(workspace=workspace, user=write_user, title="Share")
    client = Client(enforce_csrf_checks=False)
    client.force_login(write_user)
    url = f"/api/workspaces/{workspace.id}/threads/{thread.id}/share/"

    enabled = client.patch(
        url,
        data=json.dumps({"is_shared": True}),
        content_type="application/json",
    )
    membership = WorkspaceMembership.objects.get(workspace=workspace, user=write_user)
    membership.role = WorkspaceRole.READ
    membership.save(update_fields=["role"])
    disabled = client.patch(
        url,
        data=json.dumps({"is_shared": False}),
        content_type="application/json",
    )

    assert enabled.status_code == 200
    assert disabled.status_code == 200
    thread.refresh_from_db()
    assert not thread.is_shared
    assert thread.share_token is None


@pytest.mark.django_db
def test_read_thread_owner_cannot_enable_share(read_user, workspace):
    thread = Thread.objects.create(workspace=workspace, user=read_user, title="Private")
    client = Client(enforce_csrf_checks=False)
    client.force_login(read_user)

    response = client.patch(
        f"/api/workspaces/{workspace.id}/threads/{thread.id}/share/",
        data=json.dumps({"is_shared": True}),
        content_type="application/json",
    )

    assert response.status_code == 403
    thread.refresh_from_db()
    assert not thread.is_shared


@pytest.mark.django_db
@pytest.mark.parametrize("is_shared", [None, 0, 1, "", "false", [], {}])
def test_thread_share_rejects_non_boolean_values_before_role_selection(
    read_user, workspace, is_shared
):
    thread = Thread.objects.create(
        workspace=workspace, user=read_user, title="Shared", is_shared=True
    )
    client = Client(enforce_csrf_checks=False)
    client.force_login(read_user)

    response = client.patch(
        f"/api/workspaces/{workspace.id}/threads/{thread.id}/share/",
        data=json.dumps({"is_shared": is_shared}),
        content_type="application/json",
    )

    assert response.status_code == 400
    assert response.json() == {"error": "is_shared must be a boolean"}
    thread.refresh_from_db()
    assert thread.is_shared


@pytest.mark.django_db
def test_read_member_cannot_disable_another_owners_share(read_user, user, workspace):
    thread = Thread.objects.create(workspace=workspace, user=user, title="Shared", is_shared=True)
    client = Client(enforce_csrf_checks=False)
    client.force_login(read_user)

    response = client.patch(
        f"/api/workspaces/{workspace.id}/threads/{thread.id}/share/",
        data=json.dumps({"is_shared": False}),
        content_type="application/json",
    )

    assert response.status_code == 404
    thread.refresh_from_db()
    assert thread.is_shared


@pytest.mark.django_db
def test_read_owner_keeps_share_get_access(read_user, workspace):
    thread = Thread.objects.create(
        workspace=workspace, user=read_user, title="Shared", is_shared=True
    )
    client = Client(enforce_csrf_checks=False)
    client.force_login(read_user)

    response = client.get(f"/api/workspaces/{workspace.id}/threads/{thread.id}/share/")

    assert response.status_code == 200
    assert response.json()["is_shared"] is True


@pytest.mark.django_db
@pytest.mark.parametrize("lost_access", ["workspace", "upstream"])
def test_share_revocation_still_requires_live_workspace_access(
    read_user, workspace, tenant, lost_access
):
    thread = Thread.objects.create(
        workspace=workspace, user=read_user, title="Shared", is_shared=True
    )
    if lost_access == "workspace":
        WorkspaceMembership.objects.filter(workspace=workspace, user=read_user).delete()
    else:
        TenantMembership.objects.filter(user=read_user, tenant=tenant).update(
            archived_at=timezone.now()
        )
    client = Client(enforce_csrf_checks=False)
    client.force_login(read_user)

    response = client.patch(
        f"/api/workspaces/{workspace.id}/threads/{thread.id}/share/",
        data=json.dumps({"is_shared": False}),
        content_type="application/json",
    )

    assert response.status_code == 403
    thread.refresh_from_db()
    assert thread.is_shared


@pytest.mark.django_db
def test_read_write_member_can_reach_job_lifecycle_mutations(write_user, workspace, monkeypatch):
    client = Client(enforce_csrf_checks=False)
    client.force_login(write_user)
    thread = Thread.objects.create(workspace=workspace, user=write_user, title="Job")
    thread_job = ThreadJob.objects.create(
        thread=thread,
        job_type=ThreadJob.JobType.MATERIALIZATION,
        procrastinate_job_id=123,
        state=ThreadJob.State.PENDING,
    )
    deferred = AsyncMock(return_value=456)
    monkeypatch.setattr(materialize_workspace, "defer_async", deferred)
    cancelled = AsyncMock(return_value=1)
    monkeypatch.setattr("apps.workspaces.api.jobs_views.cancel_thread_job", cancelled)

    cancel_materialization = client.post(f"/api/workspaces/{workspace.id}/materialization/cancel/")
    retry_materialization = client.post(
        f"/api/workspaces/{workspace.id}/materialize/retry/",
        data=json.dumps({}),
        content_type="application/json",
    )
    cancel_job = client.post(f"/api/workspaces/{workspace.id}/jobs/{thread_job.id}/cancel/")

    assert cancel_materialization.status_code == 200
    assert retry_materialization.status_code == 200
    deferred.assert_awaited_once()
    assert cancel_job.status_code == 200
    cancelled.assert_awaited_once()


@pytest.mark.django_db
def test_read_member_can_run_recipe_and_create_audit_record(read_user, workspace, monkeypatch):
    recipe = Recipe.objects.create(
        workspace=workspace,
        created_by=read_user,
        name="Reader query",
        prompt="Count records",
        variables=[],
    )
    deferred = AsyncMock()
    monkeypatch.setattr(run_recipe, "defer_async", deferred)
    monkeypatch.setattr("apps.recipes.api.views.touch_workspace_schemas", AsyncMock())
    client = Client(enforce_csrf_checks=False)
    client.force_login(read_user)

    response = client.post(
        f"/api/workspaces/{workspace.id}/recipes/{recipe.id}/run/",
        data=json.dumps({}),
        content_type="application/json",
    )

    assert response.status_code == 202
    assert RecipeRun.objects.filter(recipe=recipe, run_by=read_user).count() == 1
    deferred.assert_awaited_once()


@pytest.mark.django_db
def test_read_member_can_run_semantic_query(read_user, workspace, monkeypatch):
    monkeypatch.setattr(
        "apps.semantic.api.views.run_semantic_query_sync",
        lambda resolved_workspace, query: {
            "success": True,
            "workspace_id": str(resolved_workspace.id),
            "query": query,
        },
    )
    client = Client(enforce_csrf_checks=False)
    client.force_login(read_user)

    response = client.post(
        f"/api/workspaces/{workspace.id}/semantic-query/",
        data=json.dumps({"measures": ["orders.count"]}),
        content_type="application/json",
    )

    assert response.status_code == 200
    assert response.json()["workspace_id"] == str(workspace.id)


@pytest.mark.django_db
def test_read_mutations_leave_existing_content_and_jobs_unchanged(
    read_user, workspace, tenant, monkeypatch
):
    artifact = Artifact.objects.create(
        workspace=workspace,
        created_by=read_user,
        title="Artifact before",
        artifact_type=ArtifactType.REACT,
        code="export default function A() {}",
        conversation_id="read-denial",
    )
    deleted_artifact = Artifact.objects.create(
        workspace=workspace,
        created_by=read_user,
        title="Deleted artifact",
        artifact_type=ArtifactType.REACT,
        code="export default function Deleted() {}",
        conversation_id="read-denial-deleted",
    )
    deleted_artifact.soft_delete(deleted_by=read_user)
    knowledge = KnowledgeEntry.objects.create(
        workspace=workspace,
        created_by=read_user,
        title="Knowledge before",
        content="Original content",
    )
    recipe = Recipe.objects.create(
        workspace=workspace,
        created_by=read_user,
        name="Recipe before",
        prompt="Query",
    )
    recipe_run = RecipeRun.objects.create(recipe=recipe, run_by=read_user)
    thread = Thread.objects.create(workspace=workspace, user=read_user, title="Thread")
    thread_job = ThreadJob.objects.create(
        thread=thread,
        job_type=ThreadJob.JobType.MATERIALIZATION,
        procrastinate_job_id=991,
        state=ThreadJob.State.PENDING,
    )
    schema = TenantSchema.objects.create(tenant=tenant, schema_name=f"role_{uuid.uuid4().hex}")
    materialization_run = MaterializationRun.objects.create(
        tenant_schema=schema,
        pipeline="commcare_sync",
        state=MaterializationRun.RunState.LOADING,
        procrastinate_job_id=991,
    )

    recovery_dispatch = AsyncMock()
    retry_dispatch = AsyncMock(return_value=992)
    materialization_cancel = AsyncMock(return_value=1)
    job_cancel = AsyncMock(return_value=1)
    monkeypatch.setattr(recover_workspace_data, "defer_async", recovery_dispatch)
    monkeypatch.setattr(materialize_workspace, "defer_async", retry_dispatch)
    monkeypatch.setattr(
        "apps.workspaces.api.materialization_views.cancel_thread_job",
        materialization_cancel,
    )
    monkeypatch.setattr("apps.workspaces.api.jobs_views.cancel_thread_job", job_cancel)

    client = Client(enforce_csrf_checks=False)
    client.force_login(read_user)
    base = f"/api/workspaces/{workspace.id}"
    responses = [
        client.post(f"{base}/artifacts/{artifact.id}/recovery/"),
        client.patch(
            f"{base}/artifacts/{artifact.id}/",
            data=json.dumps({"title": "Artifact after"}),
            content_type="application/json",
        ),
        client.delete(f"{base}/artifacts/{artifact.id}/"),
        client.post(f"{base}/artifacts/{deleted_artifact.id}/undelete/"),
        client.post(
            f"{base}/knowledge/",
            data=json.dumps({"type": "entry", "title": "Knowledge new", "content": "New content"}),
            content_type="application/json",
        ),
        client.put(
            f"{base}/knowledge/{knowledge.id}/",
            data=json.dumps({"title": "Knowledge after"}),
            content_type="application/json",
        ),
        client.delete(f"{base}/knowledge/{knowledge.id}/"),
        client.put(
            f"{base}/recipes/{recipe.id}/",
            data=json.dumps({"name": "Recipe after"}),
            content_type="application/json",
        ),
        client.patch(
            f"{base}/recipes/{recipe.id}/runs/{recipe_run.id}/",
            data=json.dumps({"is_public": True}),
            content_type="application/json",
        ),
        client.delete(f"{base}/recipes/{recipe.id}/"),
        client.post(f"{base}/materialization/cancel/"),
        client.post(
            f"{base}/materialize/retry/",
            data=json.dumps({}),
            content_type="application/json",
        ),
        client.post(f"{base}/jobs/{thread_job.id}/cancel/"),
        client.patch(
            f"{base}/threads/{thread.id}/share/",
            data=json.dumps({"is_shared": True}),
            content_type="application/json",
        ),
    ]
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("imported.md", "---\ntitle: Imported\ntags: []\n---\nImported content")
    responses.append(
        client.post(
            f"{base}/knowledge/import/",
            {
                "file": SimpleUploadedFile(
                    "knowledge.zip",
                    archive.getvalue(),
                    content_type="application/zip",
                )
            },
        )
    )

    assert all(response.status_code == 403 for response in responses)
    artifact.refresh_from_db()
    deleted_artifact.refresh_from_db()
    knowledge.refresh_from_db()
    recipe.refresh_from_db()
    recipe_run.refresh_from_db()
    thread.refresh_from_db()
    thread_job.refresh_from_db()
    materialization_run.refresh_from_db()
    assert artifact.title == "Artifact before"
    assert not artifact.is_deleted
    assert deleted_artifact.is_deleted
    assert knowledge.title == "Knowledge before"
    assert not KnowledgeEntry.objects.filter(workspace=workspace, title="Knowledge new").exists()
    assert not KnowledgeEntry.objects.filter(workspace=workspace, title="Imported").exists()
    assert recipe.name == "Recipe before"
    assert not recipe.is_deleted
    assert not recipe_run.is_public
    assert not thread.is_shared
    assert thread_job.state == ThreadJob.State.PENDING
    assert materialization_run.state == MaterializationRun.RunState.LOADING
    assert not WorkspaceDataRecovery.objects.filter(workspace=workspace).exists()
    recovery_dispatch.assert_not_awaited()
    retry_dispatch.assert_not_awaited()
    materialization_cancel.assert_not_awaited()
    job_cancel.assert_not_awaited()
