"""
URL configuration for projects app.
"""
from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .api.connections import DatabaseConnectionViewSet
from .api.csv_import import csv_import_view
from .api.data_dictionary import (
    DataDictionaryView,
    RefreshSchemaView,
    TableAnnotationsView,
)
from .api.views import (
    ProjectDetailView,
    ProjectListCreateView,
    ProjectMemberDetailView,
    ProjectMembersView,
)
from .views import ProjectListView

app_name = "projects"

router = DefaultRouter()
router.register(r"connections", DatabaseConnectionViewSet, basename="connection")

urlpatterns = [
    # Legacy view (if needed for backwards compatibility)
    path("legacy/", ProjectListView.as_view(), name="project-list"),
    # API endpoints
    path("", ProjectListCreateView.as_view(), name="list_create"),
    path("<uuid:project_id>/", ProjectDetailView.as_view(), name="detail"),
    path("<uuid:project_id>/members/", ProjectMembersView.as_view(), name="members"),
    path(
        "<uuid:project_id>/members/<uuid:user_id>/",
        ProjectMemberDetailView.as_view(),
        name="member_detail",
    ),
    # Data dictionary endpoints
    path(
        "<uuid:project_id>/data-dictionary/",
        DataDictionaryView.as_view(),
        name="data_dictionary",
    ),
    path(
        "<uuid:project_id>/refresh-schema/",
        RefreshSchemaView.as_view(),
        name="refresh_schema",
    ),
    path(
        "<uuid:project_id>/data-dictionary/tables/<str:table_path>/",
        TableAnnotationsView.as_view(),
        name="table_annotations",
    ),
    # Database connections (router adds connections/ prefix)
    path("", include(router.urls)),
    # CSV import
    path("csv-import/", csv_import_view, name="csv-import"),
]
