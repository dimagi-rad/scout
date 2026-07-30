from django.conf import settings
from django.http import HttpRequest, HttpResponse


class EmbedFrameOptionsMiddleware:
    """Allow iframe embedding for /embed/ routes from configured origins."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        response = self.get_response(request)
        if not request.path.startswith("/embed/"):
            return response

        allowed_origins = getattr(settings, "EMBED_ALLOWED_ORIGINS", [])
        if not allowed_origins:
            return response

        # X-Frame-Options conflicts with CSP frame-ancestors; drop it in favor of CSP.
        if "X-Frame-Options" in response:
            del response["X-Frame-Options"]

        origins = " ".join(allowed_origins)
        response["Content-Security-Policy"] = f"frame-ancestors 'self' {origins}"

        # Cross-origin cookie handling lives in production.py: it flips
        # SESSION/CSRF_COOKIE_SAMESITE to "None" when EMBED_ALLOWED_ORIGINS is set,
        # else cookies from the OAuth callback (outside /embed/) default to
        # SameSite=Lax and never reach the iframe.

        return response
