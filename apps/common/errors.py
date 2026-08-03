"""Errors shared across Scout's provider integrations.

Each provider auth error used to be defined **twice** as two unrelated classes —
once in ``apps/users/services/tenant_resolution.py`` and once in the matching
``mcp_server/loaders/*_base.py``. An ``except OCSAuthError`` that imported one
sailed straight past the other, which is why these reach ``except Exception``
rather than being handled (#371).

They are defined here once and imported by both. The point is *identity*, not a
shared name.
"""

from __future__ import annotations


class CommCareAuthError(Exception):
    """Raised when CommCare HQ returns a 401 or 403."""


class ConnectAuthError(Exception):
    """Raised when CommCare Connect returns a 401 or 403."""


class OCSAuthError(Exception):
    """Raised when Open Chat Studio returns a 401 or 403."""
