"""Whether a connection still reaches its sources, for Connected Accounts.

Token health alone reads "connected" for a sign-in the provider refuses: OCS
answers 401 to a freshly refreshed token whose user left the team, and Scout then
records the denial and archives the memberships while the token stays healthy.
"""

from apps.common.error_codes import ErrorCode
from apps.users.models import TenantConnection

ACCESS_OK = "ok"
# The sign-in (or API key) itself is dead: reconnecting fixes it.
ACCESS_EXPIRED = "expired"
# The provider refuses a working sign-in: an admin there must restore access.
ACCESS_REFUSED = "refused"
# Some sources are no longer reachable (denied one by one, or gone upstream);
# the rest still work.
ACCESS_PARTIAL = "partial"


def connection_access_state(
    conn: TenantConnection, *, status: str | None, live_count: int, archived_count: int
) -> str:
    """Classify ``conn``. ``status`` is :func:`aconnection_status` (None for API keys)."""
    if status == "expired":
        return ACCESS_EXPIRED
    if status == "needs_team":
        # The status badge already says what to do, and such a connection has no sources.
        return ACCESS_OK
    if conn.upstream_denial_code:
        if (
            conn.credential_type == TenantConnection.API_KEY
            and conn.upstream_denial_code == ErrorCode.AUTH_TOKEN_EXPIRED
        ):
            return ACCESS_EXPIRED
        # A 401 recorded against an OAuth sign-in that still refreshes is the
        # provider refusing it, not an expiry (the verifier retries a 401 once
        # with a refreshed token before recording it).
        return ACCESS_REFUSED
    if archived_count and not live_count:
        return ACCESS_REFUSED
    if archived_count:
        return ACCESS_PARTIAL
    return ACCESS_OK
