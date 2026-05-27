"""Task Badger initialization.

Wires the Procrastinate system integration so every ``@app.task`` decorated
function is automatically tracked. Call ``init_taskbadger`` once during Django
startup (see ``apps.workspaces.apps.WorkspacesConfig.ready``).

Disabled when ``TASKBADGER_API_KEY`` is blank.
"""

import logging

import taskbadger
from django.conf import settings
from taskbadger.systems.procrastinate import ProcrastinateSystemIntegration

from config.procrastinate import app as procrastinate_app

log = logging.getLogger(__name__)

_initialized = False


def init_taskbadger() -> None:
    """Initialize Task Badger with Procrastinate auto-tracking.

    Idempotent: subsequent calls are no-ops so this can safely be invoked from
    AppConfig.ready, which may run more than once under some Django reloader
    configurations.
    """
    global _initialized
    if _initialized or not settings.TASKBADGER_API_KEY:
        return

    taskbadger.init(
        token=settings.TASKBADGER_API_KEY,
        systems=[
            ProcrastinateSystemIntegration(
                app=procrastinate_app,
                auto_track_tasks=True,
                record_task_args=True,
            )
        ],
        tags={"environment": settings.TASKBADGER_ENVIRONMENT},
    )
    _initialized = True
    log.info("Task Badger initialized with Procrastinate integration")
