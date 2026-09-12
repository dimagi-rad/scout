"""Scope each OAuth ``TenantConnection`` to the team its credential authorises.

Replaces ``unique_oauth_connection_per_user_provider`` with
``unique_oauth_connection_per_user_provider_scope``. Dropping the old guarantee
without the narrower one would let a provider mint an unbounded number of
duplicate connections for the same team, so the two operations belong in one
migration.

The backfill points every existing OAuth connection at the allauth identity that
holds its token and, for OCS, records the team that token is scoped to (from the
uid suffix migration 0010 wrote, falling back to the ``team`` OIDC claim).
Account-wide providers keep ``scope_key=""``, for which the new constraint is
exactly the old one.

Reversible from the state it is applied to. Once a user holds two connections for
one provider the reverse hits the re-added ``unique(user, provider)`` — inherent
to un-doing multi-token support.
"""

import django.db.models.deletion
from django.db import migrations, models

UID_TEAM_SEPARATOR = "#"


def _social_accounts_for(SocialAccount, user_id, provider):
    """allauth accounts whose provider id maps to *provider*.

    ``SocialAccount.provider`` stores the configured provider *id* (e.g.
    ``commcare_prod``), not the provider class id, so this mirrors the prefix
    rules the credential resolver uses.
    """
    qs = SocialAccount.objects.filter(user_id=user_id)
    if provider == "commcare_connect":
        return qs.filter(provider__startswith="commcare_connect")
    if provider == "commcare":
        return qs.filter(provider__startswith="commcare").exclude(
            provider__startswith="commcare_connect"
        )
    if provider == "ocs":
        return qs.filter(provider__startswith="ocs")
    return qs.filter(provider=provider)


def forward(apps, schema_editor):
    TenantConnection = apps.get_model("users", "TenantConnection")
    TenantMembership = apps.get_model("users", "TenantMembership")
    SocialAccount = apps.get_model("socialaccount", "SocialAccount")

    for conn in TenantConnection.objects.filter(credential_type="oauth"):
        account = _social_accounts_for(SocialAccount, conn.user_id, conn.provider).first()
        if account is not None:
            conn.social_account = account

        if conn.provider == "ocs":
            _sub, _sep, from_uid = (account.uid if account else "").partition(UID_TEAM_SEPARATOR)
            claim = str(((account.extra_data if account else None) or {}).get("team") or "").strip()
            conn.scope_key = from_uid or claim
            if conn.scope_key:
                labelled = (
                    TenantMembership.objects.filter(connection=conn)
                    .filter(provider_metadata__team_slug=conn.scope_key)
                    .exclude(provider_metadata__team_name="")
                    .first()
                )
                conn.scope_label = (
                    (labelled.provider_metadata or {}).get("team_name")
                    if labelled
                    else conn.scope_key
                )
        conn.save(update_fields=["social_account", "scope_key", "scope_label"])


def backward(apps, schema_editor):
    """No-op: the fields are dropped by the schema reversal above this."""


class Migration(migrations.Migration):
    dependencies = [
        ("socialaccount", "0006_alter_socialaccount_extra_data"),
        ("users", "0010_qualify_ocs_social_account_uid_by_team"),
    ]

    operations = [
        migrations.AddField(
            model_name="tenantconnection",
            name="scope_key",
            field=models.CharField(
                blank=True,
                db_default="",
                default="",
                help_text=(
                    "Provider-native scope this credential authorises (OCS team slug). "
                    'Empty for providers whose tokens are account-wide. Never NULL — "" is '
                    "a real value the uniqueness constraint must collapse on."
                ),
                max_length=255,
            ),
        ),
        migrations.AddField(
            model_name="tenantconnection",
            name="scope_label",
            field=models.CharField(
                blank=True,
                db_default="",
                default="",
                help_text="Human-readable name for scope_key (e.g. the OCS team name).",
                max_length=255,
            ),
        ),
        migrations.AddField(
            model_name="tenantconnection",
            name="social_account",
            field=models.ForeignKey(
                blank=True,
                help_text=(
                    "The allauth identity holding this OAuth connection's token. Null for API keys."
                ),
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="tenant_connections",
                to="socialaccount.socialaccount",
            ),
        ),
        migrations.RunPython(forward, backward),
        migrations.RemoveConstraint(
            model_name="tenantconnection",
            name="unique_oauth_connection_per_user_provider",
        ),
        migrations.AddConstraint(
            model_name="tenantconnection",
            constraint=models.UniqueConstraint(
                condition=models.Q(("credential_type", "oauth")),
                fields=("user", "provider", "scope_key"),
                name="unique_oauth_connection_per_user_provider_scope",
            ),
        ),
    ]
