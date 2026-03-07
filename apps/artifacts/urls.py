"""
URL configuration for artifacts app.

Included at /api/artifacts/ in the main URL configuration.
"""

from django.urls import path

from .api.views import CreateShareView, ListSharesView, RevokeShareView
from .views import (
    ArtifactDataView,
    ArtifactDetailView,
    ArtifactExportView,
    ArtifactListView,
    ArtifactQueryDataView,
    ArtifactSandboxView,
    ArtifactUndeleteView,
    SharedArtifactView,
)

app_name = "artifacts"

urlpatterns = [
    # Public — no tenant required
    path("shared/<str:share_token>/", SharedArtifactView.as_view(), name="shared"),
    # Tenant-scoped routes
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
        "<uuid:tenant_id>/<uuid:artifact_id>/share/",
        CreateShareView.as_view(),
        name="create_share",
    ),
    path(
        "<uuid:tenant_id>/<uuid:artifact_id>/shares/",
        ListSharesView.as_view(),
        name="list_shares",
    ),
    path(
        "<uuid:tenant_id>/<uuid:artifact_id>/shares/<str:share_token>/",
        RevokeShareView.as_view(),
        name="revoke_share",
    ),
    path(
        "<uuid:tenant_id>/<uuid:artifact_id>/export/<str:format>/",
        ArtifactExportView.as_view(),
        name="export",
    ),
]
