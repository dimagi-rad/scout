from django.contrib.auth.signals import user_logged_in
from django.dispatch import receiver

from apps.telemetry.models import EventKind
from apps.telemetry.recorder import record


@receiver(user_logged_in, dispatch_uid="telemetry_record_login")
def record_login(sender, request, user, **kwargs):
    record(EventKind.LOGIN, user_id=user.pk)
