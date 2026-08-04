"""Scout's error taxonomy: expected operational states vs. defects.

An ``ExpectedStateError`` is dropped by ``config.sentry.before_send``. It is
still logged — it just stops minting Sentry issues and paging people.

Because raising one silences an alert, the bar is deliberately explicit. Raise
``ExpectedStateError`` only when **all four** hold:

1. **Known** — the condition can be named in advance ("the refresh token is
   dead"), not "something went wrong in here."
2. **Routine** — it occurs in the normal operation of a *correct* system. No
   change to Scout's code would prevent it.
3. **Surfaced** — somebody who can act on it is told, through a channel that is
   not Sentry.
4. **Resolved** — there is a defined next step (the user reconnects, the run
   retries), or the condition is a legitimate no-op.

(3) is the load-bearing one and the easiest to wave through — an expected state
nobody is told about is a silent failure, not an expected state. It is why the
login signal path (``apps/users/signals.py``) is deliberately not classified as
expected: a failed tenant resolution there leaves the user on an empty
data-sources page indistinguishable from "this account has no data", so the
Sentry event is the only signal anything went wrong.

If any of the four fail, raise a plain ``Exception``.
"""

from __future__ import annotations


class ExpectedStateError(Exception):
    """A known, routine operational condition — not a defect.

    Dropped from Sentry by ``config.sentry.before_send``. See the module
    docstring for the four-part test a subclass must satisfy before it inherits
    from this.
    """


class ExpectedUpstreamError(ExpectedStateError):
    """An expected state whose cause is an upstream provider, not Scout.

    ``provider`` lets a handler or a log line name the system that said no
    without re-parsing the message. It is a *class* attribute so that
    ``raise SomeAuthError("message")`` call sites need no constructor change.
    """

    provider: str | None = None


# Both apps/users/services/tenant_resolution.py and mcp_server/loaders/*_base.py
# raise these, and each must catch what the other raises — so they live here, as
# one class per provider rather than a name per module.


class CommCareAuthError(Exception):
    """Raised when CommCare HQ returns a 401 or 403."""


class ConnectAuthError(Exception):
    """Raised when CommCare Connect returns a 401 or 403."""


class OCSAuthError(Exception):
    """Raised when Open Chat Studio returns a 401 or 403."""
