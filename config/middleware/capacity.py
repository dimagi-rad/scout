from django.http import HttpRequest, HttpResponse
from django.utils.deprecation import MiddlewareMixin

from apps.common.capacity import busy_response, classify_capacity_error, report_capacity_exhausted


class CapacityExhaustedMiddleware(MiddlewareMixin):
    """Answer a view that ran out of connections with a retryable 503, not a 500.

    Last in ``MIDDLEWARE`` so its ``process_exception`` runs first. Django calls
    ``process_exception`` from a thread for async views too, so the sync alert
    path is safe here.
    """

    def process_exception(self, request: HttpRequest, exception: Exception) -> HttpResponse | None:
        capacity = classify_capacity_error(exception)
        if capacity is None:
            return None
        report_capacity_exhausted(capacity.resource)
        return busy_response()
