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

        # Remove X-Frame-Options (conflicts with CSP frame-ancestors)
        if "X-Frame-Options" in response:
            del response["X-Frame-Options"]

        # Set CSP frame-ancestors
        origins = " ".join(allowed_origins)
        response["Content-Security-Policy"] = f"frame-ancestors 'self' {origins}"

        # Patch cookies for cross-origin iframe usage
        for cookie_name in response.cookies:
            response.cookies[cookie_name]["samesite"] = "None"
            response.cookies[cookie_name]["secure"] = True

        return response
