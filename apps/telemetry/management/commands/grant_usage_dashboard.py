"""Grant or revoke the usage dashboard permission for one user, by email."""

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.management.base import BaseCommand, CommandError

from apps.telemetry.access import forget_usage_dashboard_flag
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
        try:
            permission = Permission.objects.get(
                content_type__app_label=app_label, codename=codename
            )
        except Permission.DoesNotExist as exc:
            raise CommandError("The permission is missing; run migrate first.") from exc
        has_it = user.user_permissions.filter(pk=permission.pk).exists()
        if revoke:
            if not has_it:
                self.stdout.write(f"{user.email} had no direct grant to revoke; no change.")
                self._warn_if_still_granted(user_model, user)
                return
            user.user_permissions.remove(permission)
            forget_usage_dashboard_flag(user.pk)
            self.stdout.write(f"Revoked usage dashboard access from {user.email}.")
            self._warn_if_still_granted(user_model, user)
        else:
            if has_it:
                self.stdout.write(f"{user.email} already has usage dashboard access; no change.")
                return
            user.user_permissions.add(permission)
            forget_usage_dashboard_flag(user.pk)
            self.stdout.write(f"Granted usage dashboard access to {user.email}.")
        if user.is_superuser:
            self.stdout.write("Note: superusers can see the dashboard regardless.")

    def _warn_if_still_granted(self, user_model, user):
        # A fresh instance: has_perm caches on the object it was first asked of.
        if user_model.objects.get(pk=user.pk).has_perm(USAGE_DASHBOARD_PERMISSION):
            self.stdout.write(
                f"Note: {user.email} still has access, as a superuser or through a group."
            )
