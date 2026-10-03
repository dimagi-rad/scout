"""Whether a connection still reaches its sources, for Connected Accounts.

Token health alone reads "connected" for a sign-in the provider refuses: OCS
answers 401 to a freshly refreshed token whose user left the team, and Scout then
records the denial and archives the memberships while the token stays healthy.
"""

from apps.common.error_codes import ErrorCode
from apps.users.models import TenantConnection, TenantMembership
from apps.users.services.oauth_scope import canonical_provider

ACCESS_OK = "ok"
# The sign-in (or API key) itself is dead: reconnecting fixes it.
ACCESS_EXPIRED = "expired"
# The provider refuses a working sign-in: an admin there must restore access.
ACCESS_REFUSED = "refused"
# Some sources are archived by a recorded denial and the rest still work.
ACCESS_PARTIAL = "partial"

ARCHIVED_DENIED = TenantMembership.ARCHIVED_DENIED
# Dropped from the provider's listing (bot deleted, domain left), or a disconnect or
# older row whose cause wasn't recorded: nothing an admin can fix.
ARCHIVED_UNLISTED = TenantMembership.ARCHIVED_UNLISTED


def archived_reason(membership: TenantMembership) -> str:
    """Why an archived membership is gone, as the page shows it."""
    if membership.archived_reason in (
        TenantMembership.ARCHIVED_DENIED,
        TenantMembership.ARCHIVED_DENIED_CONNECTION,
    ):
        return ARCHIVED_DENIED
    return ARCHIVED_UNLISTED


def connection_access_state(
    conn: TenantConnection, *, status: str | None, live_count: int, denied_count: int
) -> str:
    """Classify ``conn``. ``status`` is :func:`aconnection_status` (None for API keys);
    ``denied_count`` counts archived memberships whose reason is a recorded denial."""
    if status == "expired":
        return ACCESS_EXPIRED
    if status == "needs_team":
        # The status badge already says what to do, and such a connection has no sources.
        return ACCESS_OK
    if conn.upstream_denial_code == ErrorCode.AUTH_TOKEN_EXPIRED:
        # OCS tokens are team-scoped and answer 401 to a working token whose user
        # left the team; elsewhere a 401 means the sign-in or key itself is dead.
        if (
            conn.credential_type == TenantConnection.OAUTH
            and canonical_provider(conn.provider) == "ocs"
        ):
            return ACCESS_REFUSED
        return ACCESS_EXPIRED
    if conn.upstream_denial_code:
        return ACCESS_REFUSED
    if denied_count:
        return ACCESS_PARTIAL if live_count else ACCESS_REFUSED
    return ACCESS_OK
