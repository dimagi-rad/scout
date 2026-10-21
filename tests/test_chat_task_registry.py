"""The chat app's procrastinate task names and schedules are persisted in queue rows.

Queued jobs and cron bookkeeping refer to tasks by name, so a rename or a schedule
change must be deliberate.
"""

import inspect

from apps.chat import tasks as chat_tasks
from config.procrastinate import app

GC_TASK = f"{chat_tasks.__name__}.collect_chat_attachments"


def test_attachment_gc_task_is_registered_under_its_persisted_name():
    task = app.tasks[GC_TASK]
    assert str(inspect.signature(task.func)) == "(timestamp: int = 0) -> dict"
    assert task.queueing_lock == "collect_chat_attachments"


def test_attachment_gc_task_runs_hourly():
    schedules = {
        name: periodic.cron
        for (name, _), periodic in app.periodic_registry.periodic_tasks.items()
        if name.startswith(f"{chat_tasks.__name__}.")
    }
    assert schedules == {GC_TASK: "41 * * * *"}
