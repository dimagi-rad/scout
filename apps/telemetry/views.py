"""The usage dashboard API (#862), for holders of a dedicated permission only."""

from __future__ import annotations

import logging

from django.core.cache import cache
from django.db import OperationalError
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.telemetry.dashboard import DEFAULT_DAYS, MAX_DAYS, build_dashboard
from apps.telemetry.models import USAGE_DASHBOARD_PERMISSION

logger = logging.getLogger(__name__)

# The numbers move slowly; a short cache keeps reloads off the shared database.
CACHE_SECONDS = 60
# The ranges the page offers; other windows are computed fresh rather than fill the cache.
CACHED_WINDOWS = frozenset({7, 30, 90})


class CanViewUsageDashboard(BasePermission):
    """Granted per person with ``grant_usage_dashboard``; ``is_staff`` alone is not enough."""

    def has_permission(self, request, view) -> bool:
        return request.user.has_perm(USAGE_DASHBOARD_PERMISSION)


class UsageDashboardView(APIView):
    permission_classes = [IsAuthenticated, CanViewUsageDashboard]

    def get(self, request):
        try:
            days = int(request.query_params.get("days", DEFAULT_DAYS))
        except (TypeError, ValueError):
            days = DEFAULT_DAYS
        days = min(MAX_DAYS, max(1, days))
        try:
            if days not in CACHED_WINDOWS:
                return Response(build_dashboard(days=days))
            # Checked permission first; the cached aggregates are the same for every viewer.
            data = cache.get_or_set(
                f"usage_dashboard:v1:{days}", lambda: build_dashboard(days=days), CACHE_SECONDS
            )
        except OperationalError:
            # The statement timeout cancelled a query: busy database, try again shortly.
            logger.warning("Usage dashboard query cancelled for a %d-day window", days)
            return Response(
                {"error": "The usage numbers took too long. Try a shorter range or retry."},
                status=503,
            )
        return Response(data)
