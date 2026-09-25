"""Reuse workspace access decisions within one request or agent turn.

One chat turn resolves the same ``(user, workspace)`` several times: once to
build the agent graph and again at each local tool's sink check. With the
all-of gate each resolution evaluates credential readiness for every workspace
tenant, so the repeats are pure overhead. Decisions are cached only inside an
explicit scope (``WorkspaceAccessCacheMiddleware`` opens one per HTTP request)
and only for ``MAX_AGE_SECONDS``, so a long turn still re-checks and a background
worker, which opens no scope, always resolves afresh. Within a scope, repeat
calls return the same ``WorkspaceAccess`` and model instances, not fresh rows.
"""

from __future__ import annotations

import time
from contextvars import ContextVar

# Short enough that a revocation or role change mid-turn still lands on the next
# tool call a few seconds later; long enough to absorb a turn's burst of checks.
MAX_AGE_SECONDS = 10.0


class Scope(dict):
    """Cached decisions for one request. A plain mutable dict, so tasks and threads
    that copy the context (async tool runs, ``sync_to_async``) share one cache."""

    closed = False


_scope: ContextVar[Scope | None] = ContextVar("workspace_access_cache", default=None)


def open_scope() -> tuple[Scope, object]:
    scope = Scope()
    return scope, _scope.set(scope)


def detach_scope(token) -> None:
    """Restore the context variable (for threads that outlive the request)."""
    _scope.reset(token)


def close_scope(scope: Scope) -> None:
    # Closing the scope object, not resetting the variable, lets a streamed
    # response end it from wherever its body finishes.
    scope.closed = True
    scope.clear()


def _active() -> Scope | None:
    scope = _scope.get()
    return None if scope is None or scope.closed else scope


def _key(user, workspace_id, options):
    user_id = getattr(user, "pk", None)
    if user_id is None:
        return None
    return (user_id, str(workspace_id), options)


def lookup(user, workspace_id, options):
    scope = _active()
    key = _key(user, workspace_id, options)
    # Single get/pop calls: the dict is shared with sync_to_async threads.
    entry = scope.get(key) if scope is not None and key is not None else None
    if entry is None:
        return None
    stored_at, result = entry
    if time.monotonic() - stored_at > MAX_AGE_SECONDS:
        scope.pop(key, None)
        return None
    return result


def store(user, workspace_id, options, result) -> None:
    scope = _active()
    key = _key(user, workspace_id, options)
    if scope is not None and key is not None:
        scope[key] = (time.monotonic(), result)
