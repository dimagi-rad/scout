"""One dbt failure must not produce four Sentry fingerprints (#374).

The top four Scout issues by 90-day volume were all the same dbt failure logged
four different ways — 662 events, 43% of everything:

    SCOUT-DJANGO-A   184  dbt_runner        "dbt run failed: dbt run failed"
    SCOUT-DJANGO-9   172  stdout_log        raw dbt line "Encountered an error:"
    SCOUT-DJANGO-F   168  file_log          the same line, file-formatted
    SCOUT-DJANGO-1T  138  executor          "TransformStageError: Stage 'system' failed"

Only the last one carries a traceback, the stage, and the underlying dbt error,
so it is the one kept. The other three are pinned as silenced here.

Note this covers `run_dbt` only. `run_dbt_test` keeps its ERROR — see #391.
"""

from __future__ import annotations

import inspect
import logging
from unittest.mock import Mock, patch

import pytest
from sentry_sdk.integrations.logging import _IGNORED_LOGGERS as sdk_ignored_loggers

from apps.transformations.services import executor
from config.sentry import before_send
from mcp_server.services.dbt_runner import run_dbt, run_dbt_test


def _log_hint(logger_name):
    return {"log_record": logging.LogRecord(logger_name, logging.ERROR, "f", 1, "boom", None, None)}


class TestDbtLoggerFiltering:
    """dbt's own loggers must not mint Sentry events."""

    @pytest.mark.parametrize("logger_name", ["stdout_log", "file_log"])
    def test_dbt_logger_events_are_dropped(self, logger_name):
        assert before_send({"event": 1}, _log_hint(logger_name)) is None

    @pytest.mark.parametrize(
        "logger_name",
        [
            "apps.workspaces.tasks",
            "mcp_server.services.materializer",
            "apps.transformations.services.executor",
            "mcp_server.services.dbt_runner",
        ],
    )
    def test_scout_logger_events_are_kept(self, logger_name):
        """The filter must stay narrow — only third-party noise."""
        event = {"event": 1}
        assert before_send(event, _log_hint(logger_name)) is event

    def test_breadcrumbs_are_deliberately_not_suppressed(self):
        """We filter in before_send, NOT with ignore_logger.

        ignore_logger disables a logger "both in breadcrumbs and as events"
        (its own docstring), which would throw dbt's raw output away instead of
        relocating it onto the retained TransformStageError event. before_send
        only sees events, so the BreadcrumbHandler path is untouched.
        """
        for name in ("stdout_log", "file_log"):
            assert name not in sdk_ignored_loggers


class TestDbtRunnerLogLevels:
    """run_dbt/run_dbt_test RETURN their error; the caller raises and logs it."""

    def _failed_result(self, exception=None):
        res = Mock()
        res.success = False
        res.exception = exception
        res.result = []
        return res

    def test_run_dbt_failure_does_not_log_at_error(self, caplog, tmp_path):
        caplog.set_level(logging.DEBUG)

        with patch("mcp_server.services.dbt_runner.dbtRunner") as runner:
            runner.return_value.invoke.return_value = self._failed_result()
            result = run_dbt(str(tmp_path), str(tmp_path), ["stg_visits"])

        assert result["success"] is False
        assert not [r for r in caplog.records if r.levelno >= logging.ERROR], (
            "run_dbt returns its error; the executor raises and logs it. Logging "
            "at ERROR here mints a second Sentry group with less context."
        )
        assert [r for r in caplog.records if r.levelno == logging.WARNING]

    def test_the_underlying_error_is_still_reported(self, caplog, tmp_path):
        """Downgrading the level must not lose the diagnostic text."""
        caplog.set_level(logging.DEBUG)

        with patch("mcp_server.services.dbt_runner.dbtRunner") as runner:
            runner.return_value.invoke.return_value = self._failed_result(
                exception=RuntimeError('column "x" does not exist')
            )
            result = run_dbt(str(tmp_path), str(tmp_path), ["stg_visits"])

        assert 'column "x" does not exist' in result["error"]
        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert any('column "x" does not exist' in m for m in warnings)

    def test_run_dbt_test_failure_still_logs_at_error(self, caplog, tmp_path):
        """run_dbt_test is NOT downgraded — nothing downstream logs it (#391).

        _execute_stage reads only test_results["tests"] and gates its raise on
        the *run* result, so a dbt test failure raises nothing and marks the run
        COMPLETED. This ERROR is currently its only trace anywhere; downgrading
        it would make test failures silent rather than de-duplicated.
        """
        caplog.set_level(logging.DEBUG)

        with patch("mcp_server.services.dbt_runner.dbtRunner") as runner:
            runner.return_value.invoke.return_value = self._failed_result()
            result = run_dbt_test(str(tmp_path), str(tmp_path), ["stg_visits"])

        assert result["success"] is False
        assert [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_the_executor_still_logs_the_one_real_error():
    """The kept fingerprint. If this stops being an ERROR, dbt failures go dark."""
    source = inspect.getsource(executor)
    assert 'logger.exception("Transformation pipeline failed")' in source, (
        "This is the single Sentry event a dbt failure should produce — it has "
        "the traceback, the stage, and the underlying dbt error."
    )
