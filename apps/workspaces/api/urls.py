"""
URL configuration for workspace data endpoints.

Nested under /api/workspaces/<workspace_id>/
"""

from django.urls import path

from .materialization_views import materialization_cancel_view
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
]
