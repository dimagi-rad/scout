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
#
# The leaves inherit on two axes, because callers need both: by provider
# (``except OCSAuthError``) and by cause (``except UpstreamAccessDenied`` catches
# every 403 across providers — the revocation signal #378/#384 key off). The
# provider axis is load-bearing beyond ``except``: ``_summarize_error`` serialises
# ``exc.__class__.__name__`` into ``MaterializationRun.result``, so the class name
# is what survives the JSON round-trip into the user-facing summary.


class UpstreamTokenExpired(Exception):
    """HTTP 401 — the credential is dead. Reconnecting mints a working one."""


class UpstreamAccessDenied(Exception):
    """HTTP 403 — the credential is valid but has no access to this resource.

    Distinct from a 401 because **reconnecting cannot fix it**: it mints an
    identically-scoped credential that fails identically, so telling the user to
    reconnect puts them in a loop (#372). The real causes are upstream access
    removal, the resource moving to another team, or a token scoped to a
    different team — none of which re-authenticating resolves.

    Also the authoritative per-tenant revocation signal the access-revocation
    work (#378/#384) needs: ``except UpstreamAccessDenied`` catches every 403
    across all three providers.
    """


# The provider classes below are deliberately NOT expected states. They are the
# base that ``apps/users/services/tenant_resolution.py`` raises from the login
# signal, which today has NO user-facing surface at all — a failed resolution
# leaves the user on an empty data-sources page and the Sentry event is the only
# signal anything broke (#371's precondition; see rule 3 in the module
# docstring). Classifying the base would silence that path without replacing it.
#
# Expectedness therefore lives on the leaf classes, which only the loaders raise.
# The loader path DOES satisfy rule 3 — its failures reach the user through the
# materialization failure summary in chat (apps/workspaces/tasks.py:161).


class CommCareAuthError(Exception):
    """Raised when CommCare HQ refuses our credential."""

    provider = "commcare"


class CommCareTokenExpiredError(CommCareAuthError, UpstreamTokenExpired):
    """CommCare HQ returned 401 for a domain load."""


class CommCareAccessDeniedError(CommCareAuthError, UpstreamAccessDenied):
    """CommCare HQ returned 403 for a specific domain."""


class ConnectAuthError(Exception):
    """Raised when CommCare Connect refuses our credential."""

    provider = "commcare_connect"


class ConnectTokenExpiredError(ConnectAuthError, UpstreamTokenExpired):
    """Connect returned 401 for an opportunity load."""


class ConnectAccessDeniedError(ConnectAuthError, UpstreamAccessDenied):
    """Connect returned 403 for a specific opportunity."""


class OCSAuthError(Exception):
    """Raised when Open Chat Studio refuses our credential."""

    provider = "ocs"


class OCSTokenExpiredError(OCSAuthError, UpstreamTokenExpired):
    """OCS returned 401 for an experiment load."""


class OCSAccessDeniedError(OCSAuthError, UpstreamAccessDenied):
    """OCS returned 403 for a specific experiment."""
