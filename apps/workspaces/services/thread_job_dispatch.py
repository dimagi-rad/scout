"""Queue a chat-bound materialization together with the ThreadJob that resumes it."""

from asgiref.sync import sync_to_async
from django.db import transaction

from apps.chat.models import ThreadJob
from apps.workspaces.tasks import materialize_workspace


@sync_to_async
def adispatch_thread_materialization(
    *,
    thread_id,
    tool_call_id: str,
    workspace_id: str,
    user_id: str,
    load_intent: dict | None,
) -> ThreadJob:
    """Defer ``materialize_workspace`` and create its PENDING ThreadJob in one commit.

    Procrastinate's Django connector queues on Django's connection, so no worker
    can see the job before its ThreadJob exists. Queued separately, a fast job
    could finish before the row committed and the resume lookup found nothing,
    leaving the chat waiting on the janitor (#365).
    """
    with transaction.atomic():
        job = materialize_workspace.defer(
            workspace_id=str(workspace_id),
            user_id=str(user_id) if user_id else "",
            load_intent=load_intent,
        )
        return ThreadJob.objects.create(
            thread_id=thread_id,
            job_type=ThreadJob.JobType.MATERIALIZATION,
            procrastinate_job_id=getattr(job, "id", job),
            tool_call_id=tool_call_id,
            state=ThreadJob.State.PENDING,
        )
