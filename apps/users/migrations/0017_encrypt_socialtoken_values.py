"""Encrypt existing plaintext OAuth tokens in allauth's SocialToken table.

Historical models carry allauth's plain ``TextField``, so values read here are
raw column contents and nothing is encrypted twice. Rows already carrying the
ciphertext prefix are skipped, which makes both directions idempotent.

Each batch commits on its own (``atomic = False``) so a batch's row locks are
not held for the whole table rewrite; an interrupted run leaves a mixed table,
which readers handle and a re-run finishes.
"""

from django.db import migrations
from django.db.models import Q

from apps.users.token_encryption import (
    CIPHERTEXT_PREFIX,
    decrypt_token_value_strict,
    encrypt_token_value,
)

BATCH_SIZE = 500
FIELDS = ("token", "token_secret")


def _plaintext_rows():
    pending = Q()
    for name in FIELDS:
        pending |= ~Q(**{f"{name}__startswith": CIPHERTEXT_PREFIX}) & ~Q(**{name: ""})
    return pending


def _encrypted_rows():
    pending = Q()
    for name in FIELDS:
        pending |= Q(**{f"{name}__startswith": CIPHERTEXT_PREFIX})
    return pending


def _rewrite(apps, pending, transform):
    SocialToken = apps.get_model("socialaccount", "SocialToken")
    last_pk = 0
    while True:
        batch = list(
            SocialToken.objects.filter(pending, pk__gt=last_pk)
            .order_by("pk")
            .only("pk", *FIELDS)[:BATCH_SIZE]
        )
        if not batch:
            return
        for row in batch:
            for name in FIELDS:
                setattr(row, name, transform(getattr(row, name)))
        SocialToken.objects.bulk_update(batch, FIELDS)
        last_pk = batch[-1].pk


def encrypt_tokens(apps, schema_editor):
    _rewrite(apps, _plaintext_rows(), encrypt_token_value)


def decrypt_tokens(apps, schema_editor):
    _rewrite(apps, _encrypted_rows(), decrypt_token_value_strict)


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ("users", "0016_verification_attempt_tenant_scope"),
        ("socialaccount", "0006_alter_socialaccount_extra_data"),
    ]

    operations = [migrations.RunPython(encrypt_tokens, decrypt_tokens)]
