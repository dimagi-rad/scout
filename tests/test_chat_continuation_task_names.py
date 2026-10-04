"""Chat continuation re-queues workspace tasks by name, since it cannot import them."""

from apps.chat.services.continuation import FLUSH_TASK_NAME, RESUME_TASK_NAME
from apps.workspaces import tasks as workspace_tasks
from config.procrastinate import app


def test_continuation_names_the_registered_resume_and_flush_tasks():
    assert app.tasks[RESUME_TASK_NAME] is workspace_tasks.resume_thread_after_materialization
    assert app.tasks[FLUSH_TASK_NAME] is workspace_tasks.flush_pending_requests
