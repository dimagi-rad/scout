"""Local setup diagnostics without querying Cube or changing configuration."""

from django.conf import settings
from django.core.checks import Warning


def check_cube_configuration(app_configs, **kwargs):
    if not settings.DEBUG:
        return []
    missing = [
        name
        for name in ("CUBE_API_URL", "CUBE_VALIDATOR_URL", "CUBEJS_API_SECRET")
        if not getattr(settings, name, "").strip()
    ]
    if not missing:
        return []
    return [
        Warning(
            f"The semantic runtime is not configured: missing {', '.join(missing)}.",
            hint=(
                "Copy the Cube settings from .env.example into your existing .env without "
                "overwriting its other values. Start dependencies with "
                "'docker compose up -d --build --wait platform-db cube'. "
                "Scout and Cube must use the same CUBEJS_API_SECRET."
            ),
            id="semantic.W001",
        )
    ]
