"""
URL configuration for projects app.
"""
from django.urls import path

from .api.views import (
    ProjectDetailView,
    ProjectListCreateView,
    ProjectMemberDetailView,
    ProjectMembersView,
    TestConnectionView,
)
from .views import ProjectListView

app_name = "projects"

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
    path("test-connection/", TestConnectionView.as_view(), name="test_connection"),
]
