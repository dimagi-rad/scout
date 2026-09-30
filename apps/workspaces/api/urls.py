"""
URL configuration for workspace data endpoints.

Nested under /api/workspaces/<workspace_id>/
"""

from django.urls import path

from .jobs_views import active_jobs_view, cancel_job_view
from .materialization_views import materialization_cancel_view, materialization_retry_view
from .views import RefreshSchemaView

app_name = "workspace_data"

urlpatterns = [
    path("refresh/", RefreshSchemaView.as_view(), name="refresh_schema"),
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
    path("jobs/active/", active_jobs_view, name="active_jobs"),
    path(
        "jobs/<uuid:thread_job_id>/cancel/",
        cancel_job_view,
        name="cancel_job",
    ),
]
