from django.urls import path

from apps.telemetry.views import UsageDashboardView

urlpatterns = [
    path("dashboard/", UsageDashboardView.as_view(), name="usage_dashboard"),
]
