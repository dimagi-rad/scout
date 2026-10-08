"""Grant or revoke the usage dashboard permission for one user, by email."""

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.management.base import BaseCommand, CommandError

from apps.telemetry.models import USAGE_DASHBOARD_PERMISSION


class Command(BaseCommand):
    help = "Grant (or with --revoke, remove) access to the usage dashboard."

    def add_arguments(self, parser):
        parser.add_argument("email")
        parser.add_argument("--revoke", action="store_true")

    def handle(self, *args, email, revoke, **options):
        user_model = get_user_model()
        try:
            user = user_model.objects.get(email__iexact=email.strip())
        except user_model.DoesNotExist as exc:
            raise CommandError(f"No user with email {email}") from exc
        except user_model.MultipleObjectsReturned as exc:
            raise CommandError(f"More than one user matches {email}") from exc
        app_label, codename = USAGE_DASHBOARD_PERMISSION.split(".")
        permission = Permission.objects.get(content_type__app_label=app_label, codename=codename)
        has_it = user.user_permissions.filter(pk=permission.pk).exists()
        if revoke:
            if not has_it:
                self.stdout.write(f"{user.email} did not have usage dashboard access; no change.")
                return
            user.user_permissions.remove(permission)
            self.stdout.write(f"Revoked usage dashboard access from {user.email}.")
        else:
            if has_it:
                self.stdout.write(f"{user.email} already has usage dashboard access; no change.")
                return
            user.user_permissions.add(permission)
            self.stdout.write(f"Granted usage dashboard access to {user.email}.")
        if user.is_superuser:
            self.stdout.write("Note: superusers can see the dashboard regardless.")
