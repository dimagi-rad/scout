import pytest
from apps.chat.models import Thread, ThreadJob
from apps.workspaces.models import Workspace
from django.contrib.auth import get_user_model

User = get_user_model()


@pytest.mark.django_db
def test_threadjob_defaults_to_pending():
    user = User.objects.create_user(email="a@b.c", password="x")
    ws = Workspace.objects.create(name="W", created_by=user)
    thread = Thread.objects.create(workspace=ws, user=user)
    job = ThreadJob.objects.create(
        thread=thread,
        job_type=ThreadJob.JobType.MATERIALIZATION,
        procrastinate_job_id=42,
        tool_call_id="abc",
    )
    assert job.state == ThreadJob.State.PENDING
    assert job.completed_at is None


@pytest.mark.django_db
def test_threadjob_procrastinate_job_id_is_unique():
    user = User.objects.create_user(email="a@b.c", password="x")
    ws = Workspace.objects.create(name="W", created_by=user)
    thread = Thread.objects.create(workspace=ws, user=user)
    ThreadJob.objects.create(
        thread=thread, job_type="materialization",
        procrastinate_job_id=99, tool_call_id="x",
    )
    with pytest.raises(Exception):
        ThreadJob.objects.create(
            thread=thread, job_type="materialization",
            procrastinate_job_id=99, tool_call_id="y",
        )
