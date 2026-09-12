"""Qualify existing OCS ``SocialAccount.uid`` values with their team slug.

``OCSProvider.extract_uid`` now returns ``<sub>#<team>`` so each team-scoped token
gets its own ``SocialAccount``/``SocialToken`` (#156). Rows written under the old
bare-``sub`` scheme would no longer be found by allauth's ``(provider, uid)``
lookup, so a returning user would be treated as a brand-new social identity.
Rewriting them here keeps every existing OCS login matching its own account.

Rows whose ``extra_data`` carries no ``team`` claim are left alone: the new
``extract_uid`` also returns a bare ``sub`` for those, so they still match.

Reversible from the state it is applied to (at most one OCS account per user).
Once a user has authorised a *second* team the reverse would collapse both
accounts onto the same bare ``sub`` and hit ``unique(provider, uid)`` — inherent
to un-doing multi-token support, not a defect in this migration.
"""

from django.db import migrations

SEPARATOR = "#"


def forward(apps, schema_editor):
    SocialAccount = apps.get_model("socialaccount", "SocialAccount")
    for account in SocialAccount.objects.filter(provider__startswith="ocs"):
        if SEPARATOR in (account.uid or ""):
            continue
        team = str((account.extra_data or {}).get("team") or "").strip()
        if not team:
            continue
        account.uid = f"{account.uid}{SEPARATOR}{team}"
        account.save(update_fields=["uid"])


def backward(apps, schema_editor):
    SocialAccount = apps.get_model("socialaccount", "SocialAccount")
    for account in SocialAccount.objects.filter(provider__startswith="ocs"):
        sub, sep, _team = (account.uid or "").partition(SEPARATOR)
        if not sep:
            continue
        account.uid = sub
        account.save(update_fields=["uid"])


class Migration(migrations.Migration):
    dependencies = [
        ("users", "0009_alter_tenantmembership_options_and_more"),
        ("socialaccount", "0006_alter_socialaccount_extra_data"),
    ]

    operations = [
        migrations.RunPython(forward, backward),
    ]
