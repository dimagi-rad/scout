"""Stand-ins for ``_run_pipeline_with_progress`` in workspace-load tests.

A workspace load only publishes a candidate that carries an owned, COMPLETED,
fingerprinted run, so a double that returns a bare ``{"status": "completed"}``
would be (correctly) refused. This double records the run a real load would.
"""

from apps.workspaces.models import MaterializationRun
from apps.workspaces.services.load_generations import pipeline_fingerprint


def completed_pipeline_run(membership, credential, pipeline, job_id, target_schema=None, **extra):
    fingerprint = pipeline_fingerprint(pipeline, membership.tenant)
    run = MaterializationRun.objects.create(
        tenant_schema=target_schema,
        pipeline=str(getattr(pipeline, "name", "pipeline")),
        procrastinate_job_id=job_id,
        state=MaterializationRun.RunState.COMPLETED,
        result={"sources": {}, "load_fingerprint": fingerprint},
    )
    return {"status": "completed", "run_id": str(run.id), "load_fingerprint": fingerprint, **extra}


def completed_refresh_run(
    membership, credential, pipeline, *, target_schema, procrastinate_job_id=None, **_kwargs
):
    """``run_pipeline`` for the refresh path: a completed, owned, receipted run."""
    fingerprint = pipeline_fingerprint(pipeline, membership.tenant)
    run = MaterializationRun.objects.create(
        tenant_schema=target_schema,
        pipeline=str(getattr(pipeline, "name", "pipeline")),
        procrastinate_job_id=procrastinate_job_id,
        state=MaterializationRun.RunState.COMPLETED,
        result={"sources": {}, "load_fingerprint": fingerprint},
    )
    return {"status": "completed", "run_id": str(run.id), "load_fingerprint": fingerprint}
