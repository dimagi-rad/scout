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
