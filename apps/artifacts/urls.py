"""
URL configuration for artifacts app.

Nested under /api/workspaces/<workspace_id>/artifacts/
"""

from django.urls import path

from .views import (
    ArtifactDataView,
    ArtifactDetailView,
    ArtifactExportView,
    ArtifactListView,
    ArtifactQueryDataView,
    ArtifactSandboxView,
    ArtifactUndeleteView,
)

app_name = "artifacts"

urlpatterns = [
    path("", ArtifactListView.as_view(), name="list"),
    path("<uuid:artifact_id>/", ArtifactDetailView.as_view(), name="detail"),
    path(
        "<uuid:artifact_id>/undelete/",
        ArtifactUndeleteView.as_view(),
        name="undelete",
    ),
    path(
        "<uuid:artifact_id>/sandbox/",
        ArtifactSandboxView.as_view(),
        name="sandbox",
    ),
    path(
        "<uuid:artifact_id>/data/",
        ArtifactDataView.as_view(),
        name="data",
    ),
    path(
        "<uuid:artifact_id>/query-data/",
        ArtifactQueryDataView.as_view(),
        name="query_data",
    ),
    path(
        "<uuid:artifact_id>/export/<str:format>/",
        ArtifactExportView.as_view(),
        name="export",
    ),
]
