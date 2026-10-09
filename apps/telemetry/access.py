"""Who may see the usage dashboard, as /me reports it."""

from django.core.cache import cache

from apps.telemetry.models import USAGE_DASHBOARD_PERMISSION
from apps.users.services.onboarding_cache import ME_ONBOARDING_TTL


def _cache_key(user_pk) -> str:
    return f"me_usage_dashboard:{user_pk}"


async def acan_view_usage_dashboard(user) -> bool:
    """The flag /me reports, cached because /me is polled.

    It only shows the link and the route; the dashboard API checks the permission
    on every request, so a stale flag can never grant access.
    """
    key = _cache_key(user.pk)
    cached = await cache.aget(key)
    if cached is None:
        cached = await user.ahas_perm(USAGE_DASHBOARD_PERMISSION)
        await cache.aset(key, cached, ME_ONBOARDING_TTL)
    return cached


def forget_usage_dashboard_flag(user_pk) -> None:
    cache.delete(_cache_key(user_pk))
