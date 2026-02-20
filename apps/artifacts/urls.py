"""
URL configuration for artifacts app.

Included at /api/artifacts/ in the main URL configuration.
"""

from django.urls import path

from .api.views import CreateShareView, ListSharesView, RevokeShareView
from .views import (
    ArtifactDataView,
    ArtifactExportView,
    ArtifactQueryDataView,
    ArtifactSandboxView,
    SharedArtifactView,
)

app_name = "artifacts"

urlpatterns = [
    # Sandbox view - serves the HTML template for rendering artifacts in an iframe
    # Full URL: /api/artifacts/<uuid>/sandbox/
    path(
        "<uuid:artifact_id>/sandbox/",
        ArtifactSandboxView.as_view(),
        name="sandbox",
    ),
    # Data API - returns artifact code and data as JSON
    # Full URL: /api/artifacts/<uuid>/data/
    path(
        "<uuid:artifact_id>/data/",
        ArtifactDataView.as_view(),
        name="data",
    ),
    # Live query execution - runs stored SQL queries and returns fresh results
    # Full URL: /api/artifacts/<uuid>/query-data/
    path(
        "<uuid:artifact_id>/query-data/",
        ArtifactQueryDataView.as_view(),
        name="query_data",
    ),
    # Shared artifact view - public access via share token
    # Full URL: /api/artifacts/shared/<token>/
    path(
        "shared/<str:share_token>/",
        SharedArtifactView.as_view(),
        name="shared",
    ),
    # Share link management API
    # Create a new share link for an artifact
    # POST /api/artifacts/<uuid>/share/
    path(
        "<uuid:artifact_id>/share/",
        CreateShareView.as_view(),
        name="create_share",
    ),
    # List all share links for an artifact
    # GET /api/artifacts/<uuid>/shares/
    path(
        "<uuid:artifact_id>/shares/",
        ListSharesView.as_view(),
        name="list_shares",
    ),
    # Revoke (delete) a share link
    # DELETE /api/artifacts/<uuid>/shares/<token>/
    path(
        "<uuid:artifact_id>/shares/<str:share_token>/",
        RevokeShareView.as_view(),
        name="revoke_share",
    ),
    # Export artifact to HTML, PNG, or PDF
    # GET /api/artifacts/<uuid>/export/<format>/
    path(
        "<uuid:artifact_id>/export/<str:format>/",
        ArtifactExportView.as_view(),
        name="export",
    ),
]
