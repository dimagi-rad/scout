"""
URL configuration for artifacts app.

Included at /api/artifacts/ in the main URL configuration.
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
    path("<uuid:tenant_id>/", ArtifactListView.as_view(), name="list"),
    path("<uuid:tenant_id>/<uuid:artifact_id>/", ArtifactDetailView.as_view(), name="detail"),
    path(
        "<uuid:tenant_id>/<uuid:artifact_id>/undelete/",
        ArtifactUndeleteView.as_view(),
        name="undelete",
    ),
    path(
        "<uuid:tenant_id>/<uuid:artifact_id>/sandbox/",
        ArtifactSandboxView.as_view(),
        name="sandbox",
    ),
    path(
        "<uuid:tenant_id>/<uuid:artifact_id>/data/",
        ArtifactDataView.as_view(),
        name="data",
    ),
    path(
        "<uuid:tenant_id>/<uuid:artifact_id>/query-data/",
        ArtifactQueryDataView.as_view(),
        name="query_data",
    ),
    path(
        "<uuid:tenant_id>/<uuid:artifact_id>/export/<str:format>/",
        ArtifactExportView.as_view(),
        name="export",
    ),
]
