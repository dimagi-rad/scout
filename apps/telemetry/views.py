"""The usage dashboard API (#862), for holders of a dedicated permission only."""

from __future__ import annotations

import logging

from django.core.cache import cache
from django.db import OperationalError
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.telemetry.dashboard import DEFAULT_DAYS, build_dashboard
from apps.telemetry.models import USAGE_DASHBOARD_PERMISSION

logger = logging.getLogger(__name__)

# The numbers move slowly; a short cache keeps reloads off the shared database.
CACHE_SECONDS = 60
# The ranges the API serves (the page offers the first three).
WINDOWS = (7, 30, 90, 365)


class CanViewUsageDashboard(BasePermission):
    """Granted per person with ``grant_usage_dashboard``; ``is_staff`` alone is not enough."""

    def has_permission(self, request, view) -> bool:
        return request.user.has_perm(USAGE_DASHBOARD_PERMISSION)


class UsageDashboardView(APIView):
    permission_classes = [IsAuthenticated, CanViewUsageDashboard]

    def get(self, request):
        try:
            asked = int(request.query_params.get("days", DEFAULT_DAYS))
        except (TypeError, ValueError):
            asked = DEFAULT_DAYS
        # Snapped to an offered range, so every request can be served from the cache.
        days = min(WINDOWS, key=lambda window: abs(window - asked))
        try:
            data = cache.get_or_set(
                f"usage_dashboard:v1:{days}", lambda: build_dashboard(days=days), CACHE_SECONDS
            )
        except OperationalError:
            # Most often the statement timeout cancelling a query on a busy database.
            logger.warning("Usage dashboard could not be built for %d days", days, exc_info=True)
            return Response(
                {"error": "The usage numbers could not be loaded. Retry shortly."}, status=503
            )
        return Response(data)
