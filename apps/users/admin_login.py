"""Throttled Django-admin login form (arch #260, 11#3).

The default ``/admin/login/`` runs straight through ``ModelBackend`` with no
throttle, lockout, or 2FA — bypassing the per-email limiter that guards Scout's
own ``/api/auth/`` endpoints. This form routes admin login attempts through the
SAME ``check_rate_limit`` / ``record_attempt`` limiter (5 failures / 300s per
email), so a brute force against a superuser is rate-limited identically.
"""

from django.contrib.admin.forms import AdminAuthenticationForm
from django.core.exceptions import ValidationError

from apps.users.rate_limiting import check_rate_limit, record_attempt


class ThrottledAdminAuthenticationForm(AdminAuthenticationForm):
    """AdminAuthenticationForm that enforces Scout's per-email auth rate limit."""

    def clean(self):
        # ``username`` is the email (USERNAME_FIELD = "email").
        username = self.cleaned_data.get("username") or self.data.get("username", "")
        if username and check_rate_limit(username):
            raise ValidationError(
                "Too many login attempts. Try again later.",
                code="rate_limited",
            )
        try:
            cleaned = super().clean()
        except ValidationError:
            # A failed credential / permission check counts toward the limit.
            if username:
                record_attempt(username, success=False)
            raise
        if username:
            record_attempt(username, success=True)
        return cleaned
