"""At-rest encryption for allauth ``SocialToken.token`` and ``token_secret``.

The two columns belong to allauth's model, so rather than wrapping every reader
the fields themselves are swapped for :class:`EncryptedTokenField` when the users
app is ready. That makes the ORM the single encrypt/decrypt boundary: model
instances, ``values()`` and ``refresh_from_db()`` all see plaintext in Python.

Stored ciphertext carries :data:`CIPHERTEXT_PREFIX` so plaintext rows and
encrypted rows can coexist and be told apart. A provider token that itself began
with the prefix would be mistaken for ciphertext; none of Scout's providers issue
such tokens. Reads accept both; writes are
still plaintext so that every process running during the next deploy can
already read the ciphertext that deploy starts writing.
"""

from __future__ import annotations

import functools
import logging

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.core.exceptions import FieldError, ImproperlyConfigured
from django.db import models

logger = logging.getLogger(__name__)

CIPHERTEXT_PREFIX = "enc1:"
ENCRYPTED_FIELDS = ("token", "token_secret")
# Fernet is randomised, so no value lookup can match a stored ciphertext.
_ALLOWED_LOOKUPS = frozenset({"isnull"})


@functools.lru_cache(maxsize=4)
def _fernet(key: str) -> Fernet:
    return Fernet(key.encode())


def _current_fernet() -> Fernet:
    key = settings.DB_CREDENTIAL_KEY
    if not key:
        raise ValueError("DB_CREDENTIAL_KEY is not set in settings")
    return _fernet(key.decode() if isinstance(key, bytes) else key)


def is_encrypted(value: str | None) -> bool:
    return bool(value) and value.startswith(CIPHERTEXT_PREFIX)


def encrypt_token_value(value: str | None) -> str | None:
    """Encrypt *value* for storage. Empty and already-encrypted values pass through."""
    if not value or is_encrypted(value):
        return value
    return CIPHERTEXT_PREFIX + _current_fernet().encrypt(value.encode()).decode()


def decrypt_token_value(value: str | None) -> str | None:
    """Plaintext for a stored value; legacy plaintext rows are returned unchanged.

    An undecryptable ciphertext (a rotated, missing or malformed key) reads as
    empty so callers treat the connection as needing reconnection instead of
    sending ciphertext upstream as a bearer token. Callers comparing credentials
    must therefore never treat an empty value as a match.
    """
    if not is_encrypted(value):
        return value
    try:
        return _current_fernet().decrypt(value[len(CIPHERTEXT_PREFIX) :].encode()).decode()
    except (InvalidToken, ValueError) as exc:
        logger.error(  # noqa: TRY400 — one traceback per row would flood Sentry
            "Failed to decrypt stored OAuth token (%s) — key rotated/misconfigured or data corrupt",
            type(exc).__name__,
        )
        return ""


class EncryptedTokenField(models.TextField):
    def from_db_value(self, value, expression, connection):
        return decrypt_token_value(value)

    def get_lookup(self, lookup_name):
        if lookup_name not in _ALLOWED_LOOKUPS:
            raise FieldError(
                f"SocialToken.{self.name} is encrypted at rest; filter on other columns "
                f"and compare the decrypted value in Python instead of '{lookup_name}'."
            )
        return super().get_lookup(lookup_name)

    def deconstruct(self):
        # Report the original class so allauth's migration state sees no change.
        name, _path, args, kwargs = super().deconstruct()
        return name, "django.db.models.TextField", args, kwargs


def install_socialtoken_encryption(social_token_model) -> None:
    for name in ENCRYPTED_FIELDS:
        field = social_token_model._meta.get_field(name)
        if isinstance(field, EncryptedTokenField):
            continue
        if type(field) is not models.TextField:
            raise ImproperlyConfigured(
                f"SocialToken.{name} is {type(field).__name__}, not TextField; "
                "at-rest token encryption cannot be installed."
            )
        field.__class__ = EncryptedTokenField
