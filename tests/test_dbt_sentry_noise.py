"""One dbt failure must not produce four Sentry fingerprints (#374).

The top four Scout issues by 90-day volume were all the same dbt failure logged
four different ways — 662 events, 43% of everything:

    SCOUT-DJANGO-A   184  dbt_runner        "dbt run failed: dbt run failed"
    SCOUT-DJANGO-9   172  stdout_log        raw dbt line "Encountered an error:"
    SCOUT-DJANGO-F   168  file_log          the same line, file-formatted
    SCOUT-DJANGO-1T  138  executor          "TransformStageError: Stage 'system' failed"

Only the last one carries a traceback, the stage, and the underlying dbt error,
so it is the one kept. This commit silences the two dbt-owned loggers.
"""

from __future__ import annotations

import pytest

from config.sentry import _IGNORED_LOGGERS, install_logger_denylist


class TestDbtLoggerDenylist:
    """dbt's own loggers must not mint Sentry events."""

    @pytest.mark.parametrize("logger_name", ["stdout_log", "file_log"])
    def test_dbt_loggers_are_ignored(self, logger_name):
        assert logger_name in _IGNORED_LOGGERS

    @pytest.mark.parametrize("logger_name", ["stdout_log", "file_log"])
    def test_install_registers_them_with_sentry(self, logger_name):
        from sentry_sdk.integrations.logging import _IGNORED_LOGGERS as sentry_ignored

        install_logger_denylist()
        assert logger_name in sentry_ignored

    def test_scout_loggers_are_not_ignored(self):
        """The denylist must stay narrow — only third-party noise."""
        from sentry_sdk.integrations.logging import _IGNORED_LOGGERS as sentry_ignored

        install_logger_denylist()
        for name in (
            "apps.workspaces.tasks",
            "mcp_server.services.materializer",
            "apps.transformations.services.executor",
            "mcp_server.services.dbt_runner",
        ):
            assert name not in sentry_ignored
