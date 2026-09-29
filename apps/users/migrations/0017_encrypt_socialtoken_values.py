"""Encrypt existing plaintext OAuth tokens in allauth's SocialToken table.

Historical models carry allauth's plain ``TextField``, so values read here are
raw column contents and nothing is encrypted twice. Rows already carrying the
ciphertext prefix are skipped, which makes both directions idempotent.

Each batch is locked, rewritten and committed on its own (``atomic = False``),
so a token refreshed concurrently is never overwritten with its stale value and
no lock is held for the whole table rewrite. An interrupted run leaves a mixed
table, which readers handle and a re-run finishes. A concurrent disconnect's
bulk DELETE can still abort a batch as a deadlock victim; re-running is safe.

Deploy only once every role (API, MCP, worker) runs code that reads ciphertext
(#691): migrate runs from the API container while older roles still serve.
"""

from django.db import migrations, transaction
from django.db.models import Q

from apps.users.token_encryption import (
    CIPHERTEXT_PREFIX,
    decrypt_token_value_strict,
    encrypt_token_value,
)

BATCH_SIZE = 100
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
        with transaction.atomic():
            batch = list(
                SocialToken.objects.select_for_update()
                .filter(pending, pk__gt=last_pk)
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
