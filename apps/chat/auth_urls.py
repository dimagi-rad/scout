"""URL configuration for auth endpoints."""

from django.urls import path

from apps.chat.views import (
    csrf_view,
    disconnect_provider_view,
    login_view,
    logout_view,
    me_view,
    providers_view,
    signup_view,
)
from apps.users.views import tenant_list_view, tenant_select_view

app_name = "auth"

urlpatterns = [
    path("csrf/", csrf_view, name="csrf"),
    path("me/", me_view, name="me"),
    path("login/", login_view, name="login"),
    path("logout/", logout_view, name="logout"),
    path("providers/", providers_view, name="providers"),
    path(
        "providers/<str:provider_id>/disconnect/",
        disconnect_provider_view,
        name="disconnect-provider",
    ),
    path("signup/", signup_view, name="signup"),
    path("tenants/", tenant_list_view, name="tenant-list"),
    path("tenants/select/", tenant_select_view, name="tenant-select"),
]
