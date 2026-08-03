"""Scout's error taxonomy: expected operational states vs. defects.

Scout had no way to say *"this failure is a known state, not a bug."* Every
condition — a revoked OAuth token, a tenant that has not been materialized yet,
an LLM emitting a malformed payload — was raised as a bare exception,
``logger.exception``'d, and escalated into Sentry. Measured over 90 days, 59% of
Sentry's event volume was routine states (#386). They crowd out real defects and
page ``#scout-ops`` on every new fingerprint.

``ExpectedStateError`` is the missing distinction. Anything raised as one is
dropped by ``config.sentry.before_send``. It is still *logged* — the record
survives in CloudWatch at WARNING — it just stops minting Sentry issues and
paging people.

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

(3) is the load-bearing one and the easiest to wave through. It is why the login
signal path (``apps/users/signals.py``) is deliberately *not* classified as
expected: a failed tenant resolution there leaves the user with an empty
data-sources page indistinguishable from "this account has no data", and the
Sentry event is the only signal that anything went wrong at all.

If any of the four fail, raise a plain ``Exception``. **An expected state that
nobody is told about is not an expected state — it is a silent failure.**
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
    without re-parsing the message. Subclasses set it as a *class* attribute so
    existing ``raise SomeAuthError("message")`` call sites keep working
    unchanged.
    """

    provider: str | None = None


# --- Provider auth errors -------------------------------------------------
#
# These were each defined TWICE as unrelated classes — once in
# ``apps/users/services/tenant_resolution.py`` and once in the matching
# ``mcp_server/loaders/*_base.py`` — so an ``except OCSAuthError`` that imported
# one sailed straight past the other (#371). They are defined here once and
# imported by both: the point is identity, not a shared name.


class CommCareAuthError(Exception):
    """Raised when CommCare HQ returns a 401 or 403."""


class ConnectAuthError(Exception):
    """Raised when CommCare Connect returns a 401 or 403."""


class OCSAuthError(Exception):
    """Raised when Open Chat Studio returns a 401 or 403."""
