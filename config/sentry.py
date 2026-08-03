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
