"""Fixtures shared by the artifact data-recovery tests."""

from types import SimpleNamespace

import pytest
from django.contrib.auth.models import update_last_login
from django.contrib.auth.signals import user_logged_in
from django.db import connection
from django.test import AsyncClient

from apps.artifacts.models import Artifact, ArtifactType
from apps.users.models import Tenant, TenantMembership, User
from apps.workspaces.models import (
    Workspace,
    WorkspaceMembership,
    WorkspaceRole,
    WorkspaceTenant,
)
from tests.tenant_access import usable_connection


@pytest.fixture
def recovery_setup(db):
    tenant = Tenant.objects.create(
        provider="commcare",
        external_id="recovery-domain",
        canonical_name="Recovery Domain",
    )
    workspace = Workspace.objects.create(name="Recovery Domain")
    WorkspaceTenant.objects.create(workspace=workspace, tenant=tenant)
    user = User.objects.create_user(email="recovery@example.com", password="pass")
    TenantMembership.objects.create(
        user=user, tenant=tenant, connection=usable_connection(user, tenant.provider)
    )
    WorkspaceMembership.objects.create(
        workspace=workspace,
        user=user,
        role=WorkspaceRole.MANAGE,
    )
    artifact = Artifact.objects.create(
        workspace=workspace,
        created_by=user,
        title="Visits",
        artifact_type=ArtifactType.STORY,
        code="",
        conversation_id="recovery-thread",
        data={"story_doc": {"schema_version": 1, "blocks": []}},
        semantic_queries=[{"name": "visits", "measures": ["visits.count"]}],
    )
    client = AsyncClient()
    user_logged_in.disconnect(update_last_login)
    try:
        client.force_login(user)
    finally:
        user_logged_in.connect(update_last_login)
    return SimpleNamespace(
        tenant=tenant,
        workspace=workspace,
        user=user,
        artifact=artifact,
        client=client,
        url=f"/api/workspaces/{workspace.id}/artifacts/{artifact.id}/recovery/",
    )


@pytest.fixture
def drop_queued_rows(django_db_blocker):
    # procrastinate_jobs is unmanaged: rows committed by transactional tests outlive them.
    with django_db_blocker.unblock(), connection.cursor() as cursor:
        cursor.execute("SELECT COALESCE(MAX(id), 0) FROM procrastinate_jobs")
        (start,) = cursor.fetchone()
    yield
    with django_db_blocker.unblock(), connection.cursor() as cursor:
        cursor.execute("DELETE FROM procrastinate_jobs WHERE id > %s", [start])
