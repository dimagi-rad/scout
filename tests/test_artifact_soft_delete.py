"""Tests for artifact soft delete functionality."""

import pytest

from apps.artifacts.models import Artifact, ArtifactType


@pytest.fixture
def artifact(db, user, workspace):
    return Artifact.objects.create(
        workspace=workspace,
        created_by=user,
        title="Test Artifact",
        artifact_type=ArtifactType.REACT,
        code="export default function() { return null; }",
        conversation_id="thread_test",
    )


@pytest.mark.django_db
def test_soft_delete_sets_is_deleted(artifact):
    artifact.soft_delete(deleted_by=artifact.created_by)
    artifact.refresh_from_db()
    assert artifact.is_deleted is True
    assert artifact.deleted_at is not None
    assert artifact.deleted_by == artifact.created_by


@pytest.mark.django_db
def test_soft_deleted_artifact_hidden_from_default_queryset(artifact):
    artifact.soft_delete(deleted_by=artifact.created_by)
    assert Artifact.objects.filter(id=artifact.id).count() == 0


@pytest.mark.django_db
def test_soft_deleted_artifact_visible_via_all_objects(artifact):
    artifact.soft_delete(deleted_by=artifact.created_by)
    assert Artifact.all_objects.filter(id=artifact.id).count() == 1


@pytest.mark.django_db
def test_undelete_restores_artifact(artifact):
    artifact.soft_delete(deleted_by=artifact.created_by)
    artifact.undelete()
    artifact.refresh_from_db()
    assert artifact.is_deleted is False
