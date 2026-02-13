"""URL configuration for knowledge app."""
from django.urls import path

from .api.views import (
    KnowledgeDetailView,
    KnowledgeExportView,
    KnowledgeImportView,
    KnowledgeListCreateView,
)

app_name = "knowledge"

urlpatterns = [
    path("", KnowledgeListCreateView.as_view(), name="list_create"),
    path("export/", KnowledgeExportView.as_view(), name="export"),
    path("import/", KnowledgeImportView.as_view(), name="import"),
    path("<uuid:item_id>/", KnowledgeDetailView.as_view(), name="detail"),
]
