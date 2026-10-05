from django.db import migrations
from django.db.models import Count, Min, Q

QUEUE_HISTORY_LIMIT = 10_000


def seed_history(apps, schema_editor):
    connection = schema_editor.connection
    if "procrastinate_jobs" not in connection.introspection.table_names():
        return
    # Queue retention removes load-mode evidence. Only retained rows can prove
    # a full load; sampling by primary key bounds this one-time migration's scan.
    with connection.cursor() as cursor:
        cursor.execute(
            """
            WITH recent_jobs AS MATERIALIZED (
                SELECT id, task_name, status, args FROM procrastinate_jobs
                ORDER BY id DESC LIMIT %s
            )
            SELECT id, args->>'workspace_id' FROM recent_jobs
            WHERE task_name = 'apps.workspaces.tasks.materialize_workspace'
              AND status = 'succeeded'
              AND COALESCE(args->>'only_unserved', 'false') = 'false'
        """,
            [QUEUE_HISTORY_LIMIT],
        )
        workspace_by_job = dict(cursor.fetchall())
    ThreadJob = apps.get_model("chat", "ThreadJob")
    MaterializationRun = apps.get_model("workspaces", "MaterializationRun")
    WorkspaceLoadTiming = apps.get_model("workspaces", "WorkspaceLoadTiming")
    jobs = list(
        ThreadJob.objects.using(connection.alias)
        .filter(
            procrastinate_job_id__in=workspace_by_job,
            job_type="materialization",
            state="completed",
            started_at__isnull=False,
            materialization_preflight_failures=[],
        )
        .select_related("thread")
    )
    runs = {
        row["procrastinate_job_id"]: row
        for row in MaterializationRun.objects.using(connection.alias)
        .filter(
            procrastinate_job_id__in=[job.procrastinate_job_id for job in jobs],
        )
        .values("procrastinate_job_id")
        .annotate(
            start=Min("started_at"),
            failures=Count("id", filter=~Q(state="completed") | Q(completed_at__isnull=True)),
        )
    }
    records = []
    for job in jobs:
        run = runs.get(job.procrastinate_job_id)
        if (
            run
            and not run["failures"]
            and job.started_at > run["start"]
            and str(job.thread.workspace_id) == workspace_by_job[job.procrastinate_job_id]
        ):
            records.append(
                WorkspaceLoadTiming(
                    workspace_id=job.thread.workspace_id,
                    job_id=job.procrastinate_job_id,
                    started_at=run["start"],
                    completed_at=job.started_at,
                    succeeded=True,
                    phase_started_at=job.started_at,
                )
            )
    WorkspaceLoadTiming.objects.using(connection.alias).bulk_create(
        records,
        ignore_conflicts=True,
        batch_size=1000,
    )


class Migration(migrations.Migration):
    dependencies = [
        ("workspaces", "0021_workspaceloadtiming"),
        ("chat", "0016_resume_stream_chunk"),
    ]
    operations = [migrations.RunPython(seed_history, migrations.RunPython.noop)]
