"""Short-lived cache for the /me onboarding computation (arch #254, finding 07#4).

The SPA polls /me; without a guard each poll re-hit all three provider APIs
(CommCare / Connect / OCS) for a token-bearing user with no persisted
memberships. The computed flag is cached briefly so a poll loop doesn't
re-resolve: long enough to throttle the poll storm, short enough that onboarding
still completes promptly. Every write that can change the answer (connecting or
removing a data source) clears the entry, so the user never waits out the TTL.
"""

ME_ONBOARDING_TTL = 30  # seconds


def me_onboarding_cache_key(user) -> str:
    return f"me_onboarding:{user.pk}"
