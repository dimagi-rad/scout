"""Sentry event filtering.

The SDK's default ``LoggingIntegration`` promotes every ERROR-level log record
into an event, so log levels alone cannot keep known operational states out of
the issue stream: one failure is often logged by two layers, each minting its own
fingerprint. ``before_send`` is the backstop — it drops classified states however
many layers log them, and it lives in the repo, in review, next to the code that
raises, rather than in a Sentry-side alert filter.
"""

from __future__ import annotations

from sentry_sdk.integrations.logging import ignore_logger

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
# ``ignore_logger`` stops them minting events; they still arrive as breadcrumbs
# on the real event, which is where dbt's output is actually useful.
_IGNORED_LOGGERS = ("stdout_log", "file_log")


def install_logger_denylist() -> None:
    """Stop third-party loggers in ``_IGNORED_LOGGERS`` from creating events."""
    for name in _IGNORED_LOGGERS:
        ignore_logger(name)


def before_send(event, hint):
    """Drop events whose exception is a known operational state.

    Only the exception actually raised is inspected — the chain
    (``__cause__`` / ``__context__``) deliberately is not. A bug that occurs
    *while handling* an expected state is still a bug, and walking the chain
    would swallow it. Events with no exception attached (a bare
    ``logger.error``) have nothing to classify, so they are always kept.
    """
    exc_info = hint.get("exc_info")
    if exc_info and isinstance(exc_info[1], ExpectedStateError):
        return None
    return event
