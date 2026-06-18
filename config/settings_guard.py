"""Fail-fast guard for the DJANGO_SETTINGS_MODULE environment variable.

The asgi / wsgi / manage entrypoints previously fell back to
``config.settings.development`` via ``os.environ.setdefault`` when
DJANGO_SETTINGS_MODULE was unset. In a production/labs deployment a dropped env
var would then silently boot the process with the permissive *development*
settings (DEBUG=True, insecure cookies). The MCP entrypoint already refuses to
start in that case; this helper gives the Django entrypoints the same posture.

Kept dependency-free (no Django imports) so it can run before ``django.setup()``
and be unit-tested without a settings load.
"""


def require_settings_module(environ) -> str:
    """Return DJANGO_SETTINGS_MODULE or raise if it is unset/empty.

    Args:
        environ: a mapping (e.g. ``os.environ``).

    Returns:
        The configured settings module name.

    Raises:
        RuntimeError: if DJANGO_SETTINGS_MODULE is missing or empty.
    """
    module = environ.get("DJANGO_SETTINGS_MODULE")
    if not module:
        raise RuntimeError(
            "DJANGO_SETTINGS_MODULE environment variable is required. "
            "Set it explicitly (e.g. 'config.settings.production', "
            "'config.settings.connectlabs', or 'config.settings.development'). "
            "Refusing to default to development settings to avoid accidentally "
            "running with DEBUG=True / insecure cookies in production."
        )
    return module
