"""Tests for the purge_deleted_artifacts command (arch #254, finding 09#9).

Soft-delete never freed rows; this command physically purges artifacts
soft-deleted past a retention window while keeping a grace period.
"""

from datetime import timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.utils import timezone

from apps.artifacts.models import Artifact, ArtifactType
from apps.users.models import Tenant
from apps.workspaces.models import Workspace, WorkspaceTenant


@pytest.fixture
def workspace(db):
    tenant = Tenant.objects.create(provider="commcare", external_id="t", canonical_name="T")
    ws = Workspace.objects.create(name="T")
    WorkspaceTenant.objects.create(workspace=ws, tenant=tenant)
    return ws


def _make(workspace, *, is_deleted, deleted_days_ago=None):
    a = Artifact.objects.create(
        workspace=workspace,
        title="A",
        artifact_type=ArtifactType.REACT,
        code="x",
        conversation_id="t",
    )
    if is_deleted:
        a.is_deleted = True
        a.deleted_at = timezone.now() - timedelta(days=deleted_days_ago or 0)
        a.save(update_fields=["is_deleted", "deleted_at"])
    return a


@pytest.mark.django_db
def test_purges_only_old_soft_deleted(workspace):
    old = _make(workspace, is_deleted=True, deleted_days_ago=40)
    recent = _make(workspace, is_deleted=True, deleted_days_ago=5)
    live = _make(workspace, is_deleted=False)

    call_command(
        "purge_deleted_artifacts", "--confirm", "--retention-days", "30", stdout=StringIO()
    )

    ids = set(Artifact.all_objects.values_list("id", flat=True))
    assert old.id not in ids, "old soft-deleted artifact must be purged"
    assert recent.id in ids, "recently-deleted artifact is within the grace period"
    assert live.id in ids, "live artifact must never be purged"


@pytest.mark.django_db
def test_dry_run_deletes_nothing(workspace):
    old = _make(workspace, is_deleted=True, deleted_days_ago=40)

    out = StringIO()
    call_command("purge_deleted_artifacts", "--retention-days", "30", stdout=out)

    assert Artifact.all_objects.filter(id=old.id).exists(), "dry run must not delete"
    assert "Dry run" in out.getvalue()
