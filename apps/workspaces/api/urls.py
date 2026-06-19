"""
URL configuration for workspace data endpoints.

Nested under /api/workspaces/<workspace_id>/
"""

from django.urls import path

from .crossopp_views import CrossOppDashboardView, CrossOppInspectorView
from .jobs_views import active_jobs_view, cancel_job_view
from .materialization_views import materialization_cancel_view, materialization_retry_view
from .views import DataDictionaryView, RefreshSchemaView, RefreshStatusView, TableDetailView

app_name = "data_dictionary"

urlpatterns = [
    path("data-dictionary/", DataDictionaryView.as_view(), name="data_dictionary"),
    path(
        "data-dictionary/tables/<str:qualified_name>/",
        TableDetailView.as_view(),
        name="table_detail",
    ),
    path("refresh/", RefreshSchemaView.as_view(), name="refresh_schema"),
    path("refresh/status/", RefreshStatusView.as_view(), name="refresh_status"),
    path(
        "materialization/cancel/",
        materialization_cancel_view,
        name="materialization_cancel",
    ),
    path(
        "materialize/retry/",
        materialization_retry_view,
        name="materialization_retry",
    ),
    path("crossopp/inspector/", CrossOppInspectorView.as_view(), name="crossopp_inspector"),
    path("crossopp/dashboard/", CrossOppDashboardView.as_view(), name="crossopp_dashboard"),
    path("jobs/active/", active_jobs_view, name="active_jobs"),
    path(
        "jobs/<uuid:thread_job_id>/cancel/",
        cancel_job_view,
        name="cancel_job",
    ),
]
