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

from apps.common.errors import ExpectedStateError

# Third-party loggers whose ERROR records are never Scout defects.
#
# dbt owns these two (``dbt_common/events/logger.py``) and writes every event it
# fires to them at the record's own level, so a failed `dbt run` emits its
# "Encountered an error:" banner down BOTH — one formatted for stdout, one for
# its log file. They are not Scout's logger hierarchy and are invisible to
# config/settings LOGGING (dbt sets ``propagate = False`` and clears handlers),
# but sentry-sdk patches ``Logger.callHandlers`` itself, so every line became an
# event regardless. That was 340 events over 90 days for text that is already in
# the exception Scout raises around the same failure (#374).
#
# Filtered here rather than with ``sentry_sdk.integrations.logging.ignore_logger``
# deliberately. ``ignore_logger`` suppresses a logger *entirely* — its docstring
# is explicit that it disables "both in breadcrumbs and as events" — which would
# discard dbt's raw output rather than relocate it. Dropping the event in
# ``before_send`` leaves the separate BreadcrumbHandler path untouched, so the
# same lines still attach to the retained TransformStageError event as
# breadcrumbs, which is where dbt's output is actually worth having.
_NOISY_LOGGERS = frozenset({"stdout_log", "file_log"})


def before_send(event, hint):
    """Drop events that carry no signal Scout should act on.

    Two rules:

    1. The raised exception is a known operational state. Only the exception
       actually raised is inspected — the chain (``__cause__`` /
       ``__context__``) deliberately is not, because a bug that occurs *while
       handling* an expected state is still a bug, and chain-walking would
       swallow it.
    2. The record came from a third-party logger in ``_NOISY_LOGGERS``.

    Everything else is kept, including a bare ``logger.error`` from Scout code
    with no exception attached: classification is a property of an exception
    type, and there is nothing to classify there.
    """
    exc_info = hint.get("exc_info")
    if exc_info and isinstance(exc_info[1], ExpectedStateError):
        return None
    record = hint.get("log_record")
    if record is not None and record.name in _NOISY_LOGGERS:
        return None
    return event
