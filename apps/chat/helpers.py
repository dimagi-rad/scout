"""Shared async helpers for chat views."""

from asgiref.sync import sync_to_async


@sync_to_async
def get_user_if_authenticated(request):
    """Access request.user (triggers sync session load) from async context."""
    if request.user.is_authenticated:
        return request.user
    return None
