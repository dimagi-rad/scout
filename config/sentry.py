"""Sentry event filtering.

The SDK's default ``LoggingIntegration`` promotes every ERROR-level log record
into an event, so log levels alone cannot keep known operational states out of
the issue stream: one failure is often logged by two layers, each minting its own
fingerprint. ``before_send`` is the backstop — it drops classified states however
many layers log them, and it lives in the repo, in review, next to the code that
raises, rather than in a Sentry-side alert filter.
"""

from __future__ import annotations

from apps.common.errors import ExpectedStateError


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
