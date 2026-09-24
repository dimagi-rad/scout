"""Stand-ins for ``_run_pipeline_with_progress`` in workspace-load tests.

A workspace load only publishes a candidate that carries an owned, COMPLETED,
fingerprinted run, so a double that returns a bare ``{"status": "completed"}``
would be (correctly) refused. This double records the run a real load would.
"""

from unittest.mock import patch

import pytest
from django.utils import timezone

from apps.workspaces.models import MaterializationRun
from apps.workspaces.services.load_generations import pipeline_fingerprint


def completed_pipeline_run(membership, credential, pipeline, job_id, target_schema, **extra):
    fingerprint = pipeline_fingerprint(pipeline, membership.tenant)
    run = MaterializationRun.objects.create(
        tenant_schema=target_schema,
        pipeline=str(getattr(pipeline, "name", "pipeline")),
        procrastinate_job_id=job_id,
        state=MaterializationRun.RunState.COMPLETED,
        completed_at=timezone.now(),
        result={"sources": {}, "load_fingerprint": fingerprint},
    )
    # extra first: the gate-critical keys below must not be overridable by accident.
    return {**extra, "status": "completed", "run_id": str(run.id), "load_fingerprint": fingerprint}


@pytest.fixture
def no_candidate_ddl():
    """Orchestration tests don't need a physical candidate schema; candidate DDL
    is covered against real managed PostgreSQL in test_shared_workspace_loads_managed."""
    with (
        patch("apps.workspaces.tasks.SchemaManager.create_physical_schema", return_value=None),
        patch("apps.workspaces.tasks.SchemaManager.teardown", return_value=None),
    ):
        yield
