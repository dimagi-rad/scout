from django.urls import path

from apps.workspace.api.views import (
    CustomWorkspaceDetailView,
    CustomWorkspaceEnterView,
    CustomWorkspaceListCreateView,
    CustomWorkspaceTenantDeleteView,
    CustomWorkspaceTenantListCreateView,
    WorkspaceMemberDetailView,
    WorkspaceMemberListCreateView,
)

app_name = "custom_workspaces"

urlpatterns = [
    path("", CustomWorkspaceListCreateView.as_view(), name="list-create"),
    path("<uuid:workspace_id>/", CustomWorkspaceDetailView.as_view(), name="detail"),
    path("<uuid:workspace_id>/enter/", CustomWorkspaceEnterView.as_view(), name="enter"),
    path(
        "<uuid:workspace_id>/tenants/",
        CustomWorkspaceTenantListCreateView.as_view(),
        name="tenants",
    ),
    path(
        "<uuid:workspace_id>/tenants/<uuid:tenant_id>/",
        CustomWorkspaceTenantDeleteView.as_view(),
        name="tenant-delete",
    ),
    path(
        "<uuid:workspace_id>/members/",
        WorkspaceMemberListCreateView.as_view(),
        name="members",
    ),
    path(
        "<uuid:workspace_id>/members/<uuid:member_id>/",
        WorkspaceMemberDetailView.as_view(),
        name="member-detail",
    ),
]
