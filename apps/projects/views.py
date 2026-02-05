"""
Views for projects app.
"""
from django.http import JsonResponse


def health_check(request):
    """
    Simple health check endpoint that returns a 200 JSON response.
    Used by Docker health checks and load balancers.
    """
    return JsonResponse({"status": "ok"})
