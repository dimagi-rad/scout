"""Authentication decorators and mixins for API views."""

from functools import wraps

from asgiref.sync import sync_to_async
from django.http import JsonResponse

_AUTH_REQUIRED = {"error": "Authentication required"}


@sync_to_async
def get_user_if_authenticated(request):
    """Access request.user (triggers sync session load) from async context."""
    if request.user.is_authenticated:
        return request.user
    return None


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
