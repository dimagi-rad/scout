"""The usage dashboard API (#862), for holders of a dedicated permission only."""

from __future__ import annotations

from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.telemetry.dashboard import DEFAULT_DAYS, MAX_DAYS, build_dashboard
from apps.telemetry.models import USAGE_DASHBOARD_PERMISSION


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
        return Response(build_dashboard(days=min(MAX_DAYS, max(1, days))))
