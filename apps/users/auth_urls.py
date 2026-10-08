"""URL configuration for auth endpoints."""

from django.urls import path

from apps.users.auth_views import (
    csrf_view,
    disconnect_provider_view,
    last_workspace_view,
    login_view,
    logout_view,
    me_view,
    providers_view,
)
from apps.users.ocs_team_views import (
    ocs_teams_connect_all_view,
    ocs_teams_dismiss_view,
    ocs_teams_stop_view,
    ocs_teams_view,
)
from apps.users.views import (
    api_key_providers_view,
    connection_detail_view,
    tenant_credential_list_view,
    tenant_ensure_view,
    tenant_list_view,
    tenant_select_view,
)

app_name = "auth"

urlpatterns = [
    path("csrf/", csrf_view, name="csrf"),
    path("me/", me_view, name="me"),
    path("last-workspace/", last_workspace_view, name="last-workspace"),
    path("login/", login_view, name="login"),
    path("logout/", logout_view, name="logout"),
    path("providers/", providers_view, name="providers"),
    path(
        "providers/<str:provider_id>/disconnect/",
        disconnect_provider_view,
        name="disconnect-provider",
    ),
    path("tenants/", tenant_list_view, name="tenant-list"),
    path("tenants/select/", tenant_select_view, name="tenant-select"),
    path("tenants/ensure/", tenant_ensure_view, name="tenant-ensure"),
    path("connections/", tenant_credential_list_view, name="connections"),
    path(
        "connections/<str:connection_id>/",
        connection_detail_view,
        name="connection-detail",
    ),
    path("ocs/teams/", ocs_teams_view, name="ocs-teams"),
    path("ocs/teams/connect-all/", ocs_teams_connect_all_view, name="ocs-teams-connect-all"),
    path("ocs/teams/stop/", ocs_teams_stop_view, name="ocs-teams-stop"),
    path("ocs/teams/dismiss/", ocs_teams_dismiss_view, name="ocs-teams-dismiss"),
    path("api-key-providers/", api_key_providers_view, name="api-key-providers"),
]
