"""Pipeline stand-ins that record the run a real load would.

Promotion only publishes a candidate that carries an owned, COMPLETED,
fingerprinted run, so a double returning a bare ``{"status": "completed"}``
would be (correctly) refused. ``completed_pipeline_run`` stands in for
``_run_pipeline_with_progress`` (workspace loads), ``completed_refresh_run``
for ``run_pipeline`` (standalone refresh).
"""

from apps.workspaces.models import MaterializationRun
from apps.workspaces.services.load_generations import pipeline_fingerprint


def completed_pipeline_run(membership, credential, pipeline, job_id, target_schema, **extra):
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
    membership, credential, pipeline, *, target_schema, procrastinate_job_id, **_kwargs
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
