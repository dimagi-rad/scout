from django.db import migrations


def deduplicate_memberships(apps, schema_editor):
    """Remove duplicate TenantMembership rows, keeping the earliest created one."""
    TenantMembership = apps.get_model("users", "TenantMembership")
    from django.db.models import Count

    dupes = (
        TenantMembership.objects.values("user_id", "tenant_id")
        .annotate(cnt=Count("id"))
        .filter(cnt__gt=1)
    )
    for dupe in dupes:
        keep = (
            TenantMembership.objects.filter(
                user_id=dupe["user_id"],
                tenant_id=dupe["tenant_id"],
            )
            .order_by("created_at")
            .first()
        )
        if keep:
            TenantMembership.objects.filter(
                user_id=dupe["user_id"],
                tenant_id=dupe["tenant_id"],
            ).exclude(id=keep.id).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("users", "0003_convert_empty_emails_to_null"),
    ]

    operations = [
        migrations.RunPython(deduplicate_memberships, migrations.RunPython.noop),
    ]
