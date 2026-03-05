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
    path("<uuid:tenant_id>/", KnowledgeListCreateView.as_view(), name="list_create"),
    path("<uuid:tenant_id>/export/", KnowledgeExportView.as_view(), name="export"),
    path("<uuid:tenant_id>/import/", KnowledgeImportView.as_view(), name="import"),
    path("<uuid:tenant_id>/<uuid:item_id>/", KnowledgeDetailView.as_view(), name="detail"),
]
