"""Tests for artifact creator attribution after user deletion."""

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
def test_artifact_survives_user_deletion(workspace, artifact):
    artifact.created_by.delete()
    artifact.refresh_from_db()
    assert artifact.created_by is None
