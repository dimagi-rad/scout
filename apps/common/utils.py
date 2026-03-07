"""Shared utility functions for the Scout platform."""


def creator_display_name(user) -> str:
    """Return display name for a content creator, handling deleted accounts."""
    if user is None:
        return "Deleted user"
    return user.get_full_name()
