"""The users app's procrastinate task names and schedules are persisted in queue rows.

Queued jobs and cron bookkeeping refer to tasks by name, so a rename or a schedule
change must be deliberate.
"""

import inspect

from apps.users import tasks as users_tasks
from config.procrastinate import app

SWEEP_TASK = f"{users_tasks.__name__}.schedule_ocs_api_key_probes"
PROBE_TASK = f"{users_tasks.__name__}.probe_ocs_chatbots"


def test_ocs_tasks_are_registered_under_their_persisted_names():
    sweep = app.tasks[SWEEP_TASK]
    assert str(inspect.signature(sweep.func)) == "(timestamp: int = 0) -> None"
    assert sweep.queueing_lock == "schedule_ocs_api_key_probes"
    assert str(inspect.signature(app.tasks[PROBE_TASK].func)) == "(connection_id: str) -> None"


def test_ocs_api_key_sweep_runs_hourly():
    schedules = {
        name: periodic.cron
        for (name, _), periodic in app.periodic_registry.periodic_tasks.items()
        if name.startswith(f"{users_tasks.__name__}.")
    }
    assert schedules == {SWEEP_TASK: "29 * * * *"}
