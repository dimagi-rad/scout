"""Request-body parsing shared by the plain Django JSON views."""

import json

from django.http import HttpRequest, JsonResponse


def parse_json_object(
    request: HttpRequest, *, allow_empty: bool = False
) -> tuple[dict | None, JsonResponse | None]:
    """Decode ``request.body`` as a JSON object, returning ``(body, None)`` or
    ``(None, error_response)``.

    ``json.loads`` happily returns a list, string or number, and callers go on to
    ``.get()`` it — so a well-formed but non-object body used to surface as a 500.
    ``allow_empty`` treats an empty or whitespace-only body as ``{}`` for endpoints
    where every field is optional.
    """
    if allow_empty and not request.body.strip():
        return {}, None
    try:
        body = json.loads(request.body)
    except (ValueError, RecursionError):
        # ValueError covers JSONDecodeError and UnicodeDecodeError.
        return None, JsonResponse({"error": "Invalid JSON"}, status=400)
    if not isinstance(body, dict):
        return None, JsonResponse({"error": "Request body must be a JSON object."}, status=400)
    return body, None


def string_field(body: dict, key: str, default: str = "") -> tuple[str | None, JsonResponse | None]:
    """Read ``body[key]`` as a string, returning ``(value, None)`` or ``(None, error_response)``.

    A missing key gives ``default``. Any other type, ``null`` included, is a 400 instead of the
    AttributeError a caller's ``.strip()`` would raise.
    """
    value = body.get(key, default)
    if not isinstance(value, str):
        return None, JsonResponse({"error": f"{key} must be a string."}, status=400)
    return value, None
