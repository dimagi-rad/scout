"""Sentry event filtering.

Scout's Sentry config had no ``before_send``, so the SDK's default
``LoggingIntegration`` promoted *every* ERROR-level log record into an event —
making the issue stream only as accurate as the codebase's log levels. Log
levels alone cannot carry the load: one failure is logged by two layers (loader
and task), each minting its own fingerprint, and the ``#scout-ops`` alert rule
fires on first-seen with ``actionMatch: any`` and no filters. So each new
variant pages.

``before_send`` is the backstop: a condition Scout has classified as a known
operational state never becomes an issue, regardless of how many layers log it.
Dropping happens here rather than in a Sentry-side alert filter so the rule
lives in the repo, in review, next to the code that raises.
"""

from __future__ import annotations

from sentry_sdk.integrations.logging import ignore_logger

from apps.common.errors import ExpectedStateError

# dbt owns these two (``dbt_common/events/logger.py``) and writes every event it
# fires to both, so one failed `dbt run` sends its "Encountered an error:" banner
# down each — 340 events over 90 days for text Scout already raises as
# TransformStageError (#374). They sit outside Scout's logger hierarchy and
# ignore config/settings LOGGING (dbt sets ``propagate = False`` and clears its
# handlers), but sentry-sdk patches ``Logger.callHandlers``, so every line became
# an event anyway.
_DBT_LOGGERS = ("stdout_log", "file_log")


def ignore_noisy_loggers():
    """Silence third-party loggers that only restate a failure Scout already reports.

    ``ignore_logger`` drops breadcrumbs as well as events, so dbt's raw
    multi-line output leaves Sentry entirely. The error *text* survives —
    ``run_dbt`` logs it at WARNING and the executor puts it in the
    TransformStageError message — so what goes is the duplicate, not the
    diagnosis. dbt's full output stays in the worker logs.
    """
    for name in _DBT_LOGGERS:
        ignore_logger(name)


def before_send(event, hint):
    """Drop events whose exception is a known operational state.

    Only the exception actually raised is inspected — the chain
    (``__cause__`` / ``__context__``) deliberately is not. A bug that occurs
    *while handling* an expected state is still a bug, and walking the chain
    would swallow it.

    Events with no exception attached (a bare ``logger.error``) are always kept:
    classification is a property of an exception type, and there is nothing to
    classify here.
    """
    exc_info = hint.get("exc_info")
    if exc_info and isinstance(exc_info[1], ExpectedStateError):
        return None
    return event
