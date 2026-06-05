from django.db import migrations


def forward(apps, schema_editor):
    """Map each existing TenantCredential onto a TenantConnection.

    OAuth credentials collapse into one connection per (user, provider).
    API-key credentials become one connection each (Fernet ciphertext can't be
    deduped to recover which chatbots shared a key). Legacy team is unknown, so
    team_slug/team_name stay "" and the OAuth team guard is skipped until the
    user re-authenticates or re-adds the key.
    """
    TenantCredential = apps.get_model("users", "TenantCredential")
    TenantConnection = apps.get_model("users", "TenantConnection")

    oauth_cache = {}  # (user_id, provider) -> connection
    for cred in TenantCredential.objects.select_related(
        "tenant_membership", "tenant_membership__tenant", "tenant_membership__user"
    ).all():
        tm = cred.tenant_membership
        provider = tm.tenant.provider
        if cred.credential_type == "oauth":
            key = (tm.user_id, provider)
            conn = oauth_cache.get(key)
            if conn is None:
                conn = TenantConnection.objects.create(
                    user=tm.user,
                    provider=provider,
                    credential_type="oauth",
                    encrypted_credential="",
                )
                oauth_cache[key] = conn
        else:
            conn = TenantConnection.objects.create(
                user=tm.user,
                provider=provider,
                credential_type="api_key",
                encrypted_credential=cred.encrypted_credential,
            )
        tm.connection = conn
        tm.save(update_fields=["connection"])


def reverse(apps, schema_editor):
    TenantCredential = apps.get_model("users", "TenantCredential")
    TenantMembership = apps.get_model("users", "TenantMembership")
    for tm in TenantMembership.objects.select_related("connection").all():
        conn = tm.connection
        if conn is None:
            continue
        TenantCredential.objects.update_or_create(
            tenant_membership=tm,
            defaults={
                "credential_type": conn.credential_type,
                "encrypted_credential": conn.encrypted_credential,
            },
        )


class Migration(migrations.Migration):
    dependencies = [("users", "0006_tenant_connections")]
    operations = [migrations.RunPython(forward, reverse)]
