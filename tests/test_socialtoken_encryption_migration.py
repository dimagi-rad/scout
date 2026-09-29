"""``users.0017`` encrypts legacy plaintext SocialToken rows, idempotently and reversibly.

The RunPython functions run against the historical app registry (plain
``TextField``s), exactly as ``migrate`` runs them, on rows seeded as raw SQL so
the live model's encrypting field cannot mask a migration that did nothing.
"""

import importlib
import math

import pytest
from allauth.socialaccount.models import SocialAccount, SocialToken
from cryptography.fernet import Fernet, InvalidToken
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import override_settings
from django.test.utils import CaptureQueriesContext

from apps.users.token_encryption import CIPHERTEXT_PREFIX, encrypt_token_value

migration = importlib.import_module("apps.users.migrations.0017_encrypt_socialtoken_values")
TARGET = ("users", "0017_encrypt_socialtoken_values")


@pytest.fixture
def historical_apps():
    executor = MigrationExecutor(connection)
    return executor.loader.project_state([TARGET]).apps


TOKEN_TABLE = SocialToken._meta.db_table


def _raw(token_pk):
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT token, token_secret FROM {TOKEN_TABLE} WHERE id = %s", [token_pk])
        return cursor.fetchone()


def _seed(user, uid, access, refresh):
    account = SocialAccount.objects.create(user=user, provider="commcare", uid=uid)
    token = SocialToken.objects.create(account=account)
    with connection.cursor() as cursor:
        cursor.execute(
            f"UPDATE {TOKEN_TABLE} SET token = %s, token_secret = %s WHERE id = %s",
            [access, refresh, token.pk],
        )
    return token.pk


@pytest.fixture
def mixed_rows(user):
    already = encrypt_token_value("enc-access")
    return {
        "plain": _seed(user, "plain", "plain-access", "plain-refresh"),
        "no_refresh": _seed(user, "no-refresh", "lonely-access", ""),
        "encrypted": _seed(user, "encrypted", already, encrypt_token_value("enc-refresh")),
        "half": _seed(user, "half", already, "half-refresh"),
    }


def _assert_all_encrypted(rows):
    for pk in rows.values():
        for value in _raw(pk):
            assert value == "" or value.startswith(CIPHERTEXT_PREFIX)


@pytest.mark.django_db
def test_forward_encrypts_every_plaintext_value(historical_apps, mixed_rows):
    untouched = _raw(mixed_rows["encrypted"])

    migration.encrypt_tokens(historical_apps, None)

    _assert_all_encrypted(mixed_rows)
    assert _raw(mixed_rows["no_refresh"])[1] == ""
    assert _raw(mixed_rows["encrypted"]) == untouched
    assert _raw(mixed_rows["half"])[0] == untouched[0]
    read = {
        name: tuple(SocialToken.objects.filter(pk=pk).values_list("token", "token_secret").get())
        for name, pk in mixed_rows.items()
    }
    assert read == {
        "plain": ("plain-access", "plain-refresh"),
        "no_refresh": ("lonely-access", ""),
        "encrypted": ("enc-access", "enc-refresh"),
        "half": ("enc-access", "half-refresh"),
    }


@pytest.mark.django_db
def test_forward_is_idempotent(historical_apps, mixed_rows):
    migration.encrypt_tokens(historical_apps, None)
    first = {pk: _raw(pk) for pk in mixed_rows.values()}

    with CaptureQueriesContext(connection) as queries:
        migration.encrypt_tokens(historical_apps, None)

    assert {pk: _raw(pk) for pk in mixed_rows.values()} == first
    statements = [q["sql"].lstrip().split()[0].upper() for q in queries]
    assert statements.count("SELECT") == 1
    assert "UPDATE" not in statements


@pytest.mark.django_db
def test_forward_batches_by_primary_key(historical_apps, user, monkeypatch):
    rows = {f"t{i}": _seed(user, f"batch-{i}", f"access-{i}", f"refresh-{i}") for i in range(5)}
    monkeypatch.setattr(migration, "BATCH_SIZE", 2)

    with CaptureQueriesContext(connection) as queries:
        migration.encrypt_tokens(historical_apps, None)

    _assert_all_encrypted(rows)
    selects = [q for q in queries if q["sql"].lstrip().upper().startswith("SELECT")]
    updates = [q for q in queries if q["sql"].lstrip().upper().startswith("UPDATE")]
    assert len(updates) == math.ceil(len(rows) / migration.BATCH_SIZE)
    assert len(selects) == len(updates) + 1
    assert all("FOR UPDATE" in q["sql"] for q in selects)


@pytest.mark.django_db
def test_reverse_restores_plaintext(historical_apps, mixed_rows):
    migration.encrypt_tokens(historical_apps, None)

    migration.decrypt_tokens(historical_apps, None)

    assert _raw(mixed_rows["plain"]) == ("plain-access", "plain-refresh")
    assert _raw(mixed_rows["no_refresh"]) == ("lonely-access", "")
    assert _raw(mixed_rows["encrypted"]) == ("enc-access", "enc-refresh")
    assert _raw(mixed_rows["half"]) == ("enc-access", "half-refresh")


@pytest.mark.django_db
def test_reverse_raises_before_writing_a_batch_it_cannot_decrypt(
    historical_apps, mixed_rows, monkeypatch
):
    migration.encrypt_tokens(historical_apps, None)
    monkeypatch.setattr(migration, "BATCH_SIZE", 2)
    before = {pk: _raw(pk) for pk in mixed_rows.values()}

    with (
        override_settings(DB_CREDENTIAL_KEY=Fernet.generate_key().decode()),
        pytest.raises(InvalidToken),
    ):
        migration.decrypt_tokens(historical_apps, None)

    assert {pk: _raw(pk) for pk in mixed_rows.values()} == before
