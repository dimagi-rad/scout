"""Shared async helpers for chat views."""

from functools import wraps

from asgiref.sync import sync_to_async
from django.http import JsonResponse

from apps.projects.models import WorkspaceMembership

_AUTH_REQUIRED = {"error": "Authentication required"}


@sync_to_async
def get_user_if_authenticated(request):
    """Access request.user (triggers sync session load) from async context."""
    if request.user.is_authenticated:
        return request.user
    return None


@sync_to_async
def _resolve_workspace_and_membership(user, workspace_id):
    """Resolve workspace access for a user.

    Returns (workspace, tenant_membership, is_multi_tenant):
    - (None, None, False): workspace not found or user lacks WorkspaceMembership
    - (workspace, None, True): multi-tenant workspace; WorkspaceMembership is sufficient
    - (workspace, None, False): single-tenant workspace but user lacks TenantMembership
    - (workspace, tm, False): single-tenant workspace with a valid TenantMembership
    """
    try:
        wm = WorkspaceMembership.objects.select_related("workspace").get(
            workspace_id=workspace_id, user=user
        )
    except WorkspaceMembership.DoesNotExist:
        return None, None, False

    workspace = wm.workspace

    # Read tenant count exactly once so callers don't need a second DB query.
    # Multi-tenant workspaces grant access by WorkspaceMembership alone;
    # TenantMembership is irrelevant (and must not be checked) for multi-tenant access.
    is_multi_tenant = workspace.workspace_tenants.count() > 1
    if is_multi_tenant:
        return workspace, None, True

    tenant = workspace.tenant
    if tenant is None:
        return workspace, None, False

    from apps.users.models import TenantMembership

    try:
        tm = TenantMembership.objects.get(user=user, tenant=tenant)
    except TenantMembership.DoesNotExist:
        return workspace, None, False
    return workspace, tm, False


def async_login_required(view_func):
    """Require authentication for async Django views. Returns 401 JSON.

    Sets request._authenticated_user so the view can access the user
    without another sync_to_async call to request.user.
    """

    @wraps(view_func)
    async def wrapper(request, *args, **kwargs):
        user = await get_user_if_authenticated(request)
        if user is None:
            return JsonResponse(_AUTH_REQUIRED, status=401)
        request._authenticated_user = user
        return await view_func(request, *args, **kwargs)

    return wrapper


def login_required_json(view_func):
    """Require authentication for sync Django views. Returns 401 JSON.

    Unlike Django's @login_required which redirects, this returns a
    JSON 401 response suitable for API endpoints.
    """

    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return JsonResponse(_AUTH_REQUIRED, status=401)
        return view_func(request, *args, **kwargs)

    return wrapper


class LoginRequiredJsonMixin:
    """Mixin for Django CBVs that returns 401 JSON instead of redirect."""

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return JsonResponse(_AUTH_REQUIRED, status=401)
        return super().dispatch(request, *args, **kwargs)
