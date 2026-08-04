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

Reporting rules
---------------

Classification says whether a failure pages. These five say how it is *reported*.
They exist because Scout had no contract here at all: a raise site and its
consumer each assumed the other's job, so remediation copy was written twice and
categories were recovered by substring-matching prose (#388 review).

1. **Every error crossing a boundary carries ``(code, message)``.** ``code`` is an
   ``ErrorCode`` — stable, never localised, never reworded — and is what handlers,
   UI, and prompts branch on. ``message`` is prose for humans and for the agent.
2. **Neither is derived from the other, and nobody parses a message.** Recovering
   a category by matching prose or a class name is a bug; see finding 06#1, where
   matching ``'"code": "NOT_FOUND"'`` broke when FastMCP's JSON separators
   changed.
3. **Raise sites describe; they never advise.** A loader says what the provider
   reported. What the user should *do* is presentation, and lives in exactly one
   place keyed by code (``_CREDENTIAL_GUIDANCE`` in ``apps/workspaces/tasks.py``).
   While both layers wrote advice, the user got it twice in two phrasings.
4. **Guidance is attributed.** One block per distinct code, naming the sources it
   applies to. A run can need opposite advice for two sources, and an
   unattributed pair reads as a contradiction (#372).
5. **Substring matching is legal only at the edge of a system we do not own.**
   ``mcp_server/services/query.py:_classify_error`` matching psycopg's
   ``"password authentication failed"`` is a boundary adapter and is fine. Between
   two halves of Scout it is never fine.
"""

from __future__ import annotations

from apps.common.error_codes import ErrorCode


class ExpectedStateError(Exception):
    """A known, routine operational condition — not a defect.

    Dropped from Sentry by ``config.sentry.before_send``. See the module
    docstring for the four-part test a subclass must satisfy before it inherits
    from this.
    """


class ExpectedUpstreamError(ExpectedStateError):
    """An expected state whose cause is an upstream provider, not Scout.

    ``provider`` and ``code`` let a handler, a log line, or a serialiser name the
    system that said no and the condition it reported without re-parsing the
    message. Both are *class* attributes so existing
    ``raise SomeAuthError("message")`` call sites keep working unchanged.
    """

    provider: str | None = None
    code: ErrorCode | None = None


# These provider auth errors were each defined TWICE as unrelated classes — once in
# ``apps/users/services/tenant_resolution.py`` and once in the matching
# ``mcp_server/loaders/*_base.py`` — so an ``except OCSAuthError`` that imported
# one sailed straight past the other (#371). They are defined here once and
# imported by both: the point is identity, not a shared name.
#
# There are two axes, because callers need both:
#
#   *provider* — OCS / CommCare / Connect.
#   *cause*    — 401 (the credential is dead) vs 403 (the credential is fine and
#       has no access to this resource). #372: these need opposite advice, and a
#       403 is an authoritative per-tenant revocation signal.
#
# The leaves inherit from both, so ``except OCSAuthError`` still catches every
# OCS auth failure and ``except UpstreamAccessDenied`` catches every 403 across
# providers.
#
# The *cause* classes carry the ``code``, because that is the axis consumers
# branch on. It is what crosses the JSON boundary into
# ``MaterializationRun.result``; the class name is not a wire value.


class UpstreamTokenExpired(ExpectedUpstreamError):
    """HTTP 401 — the credential is dead. Reconnecting mints a working one.

    Expected under the module's four-part test: known (the provider told us),
    routine (OAuth tokens expire and get revoked in normal operation), surfaced
    (the reconnect guidance reaches the user in the chat failure summary), and
    resolved (reconnect, re-run).

    Shares ``AUTH_TOKEN_EXPIRED`` with ``CredentialResolutionError``'s pre-flight
    check: one condition gets one code however it was detected.
    """

    code = ErrorCode.AUTH_TOKEN_EXPIRED


class UpstreamAccessDenied(ExpectedUpstreamError):
    """HTTP 403 — the credential is valid but has no access to this resource.

    Distinct from a 401 because **reconnecting cannot fix it**: it mints an
    identically-scoped credential that fails identically, so telling the user to
    reconnect puts them in a loop (#372). The real causes are upstream access
    removal, the resource moving to another team, or a token scoped to a
    different team — none of which re-authenticating resolves.

    Also the authoritative per-tenant revocation signal the access-revocation
    work (#378/#384) needs: ``except UpstreamAccessDenied`` catches every 403
    across all three providers.

    Expected under the module's four-part test — and note rule 3 only started
    holding for a 403 once the guidance stopped saying "reconnect" (#372).
    Classifying it before that would have silenced a condition whose only
    user-facing advice was wrong.
    """

    code = ErrorCode.AUTH_ACCESS_DENIED


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
