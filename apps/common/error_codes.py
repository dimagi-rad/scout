"""The one registry of Scout error codes.

A code is the **machine-readable** half of an error. It is stable, never
localised, and never reworded — handlers, UI, and prompts branch on it. The
message beside it is the **human-readable** half: free to change, and never
parsed.

Every error that crosses a boundary carries both. Neither is derived from the
other. Recovering a category by substring-matching a message is a bug: Scout has
already been burned by it once, when matching ``'"code": "NOT_FOUND"'`` in tool
output broke the moment FastMCP's JSON separators changed (see
``apps/agents/graph/base.py`` and finding 06#1).

These began as string constants in ``mcp_server.envelope``, which still re-exports
them so the ~30 ``error_response(...)`` call sites in ``mcp_server/server.py``
keep working. They live here because ``apps.common.errors`` attaches them to
exception classes and must not import from ``mcp_server``.
"""

from __future__ import annotations

from enum import StrEnum


class ErrorCode(StrEnum):
    """Stable error identifiers. Values are the wire format — do not rename."""

    VALIDATION_ERROR = "VALIDATION_ERROR"
    CONNECTION_ERROR = "CONNECTION_ERROR"
    QUERY_TIMEOUT = "QUERY_TIMEOUT"
    NOT_FOUND = "NOT_FOUND"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    SCHEMA_BUILD_FAILED = "SCHEMA_BUILD_FAILED"

    # No materialization pipeline could be resolved for a tenant's provider.
    # Distinct from SCHEMA_BUILD_FAILED: the schema may be perfectly healthy —
    # Scout just cannot say which loader wrote it, so it must not guess (#155).
    PIPELINE_UNRESOLVED = "PIPELINE_UNRESOLVED"

    # HTTP 401 upstream: the credential is dead and reconnecting mints a working
    # one. Shared deliberately by the loaders and by credential_resolver's
    # pre-flight check — one condition, one code, however it is detected.
    AUTH_TOKEN_EXPIRED = "AUTH_TOKEN_EXPIRED"  # noqa: S105 — error code, not a credential

    # HTTP 403 upstream: the credential is valid and has no access here.
    # Reconnecting mints an identically-scoped credential that fails the same
    # way, so this must never be collapsed into AUTH_TOKEN_EXPIRED (#372).
    AUTH_ACCESS_DENIED = "AUTH_ACCESS_DENIED"


def code_of(exc: BaseException) -> str:
    """Return the ``ErrorCode`` an exception declares, defaulting to INTERNAL_ERROR.

    Reads the class attribute rather than the class *name*: the name is a
    refactoring hazard and cannot be a stable wire value.
    """
    code = getattr(exc, "code", None)
    return str(code) if code else str(ErrorCode.INTERNAL_ERROR)
