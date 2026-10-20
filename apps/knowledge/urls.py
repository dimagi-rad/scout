"""URL configuration for knowledge app.

Nested under /api/workspaces/<workspace_id>/knowledge/
"""

from django.urls import path

from .api.views import (
    KnowledgeDetailView,
    KnowledgeExportView,
    KnowledgeImportView,
    KnowledgeListCreateView,
    KnowledgeUploadView,
)

app_name = "knowledge"

urlpatterns = [
    path("", KnowledgeListCreateView.as_view(), name="list_create"),
    path("export/", KnowledgeExportView.as_view(), name="export"),
    path("import/", KnowledgeImportView.as_view(), name="import"),
    path("upload/", KnowledgeUploadView.as_view(), name="upload"),
    path("<uuid:item_id>/", KnowledgeDetailView.as_view(), name="detail"),
]
