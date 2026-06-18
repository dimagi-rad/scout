"""Single helper for minting valid PostgreSQL identifiers (arch #235).

Every Postgres name Scout creates — tenant schema, read-only/dbt role, refresh
schema, dbt model/column alias — routes through here so the 63-byte limit
(``NAMEDATALEN - 1``) and the ``(provider, external_id)`` collision class are
guarded in exactly one place. PR #227 fixed view names only; this closes the rest.

Two guard modes:

- ``always_hash`` (``tenant_schema_name``): a digest of the identity key is ALWAYS
  woven in, so distinct keys never collide even when their sanitized bases match
  (Connect ``'123'`` vs OCS ``'123'``; ``'a-b'`` vs ``'a_b'``) or fit comfortably.
- hash-on-overflow (``readonly_role_name``, ``dbt_role_name``, dbt names): the
  plain ``{base}{suffix}`` is returned verbatim when it fits 63 bytes — preserving
  existing prod names so no data migration is needed — and a digest is woven in
  only when truncation would otherwise silently collapse two names into one.
"""

from __future__ import annotations

import hashlib

PG_MAX_IDENTIFIER_BYTES = 63
_DIGEST_LEN = 8
# Cap minted schema names below the hard limit so derived names (``_ro``,
# ``_dbt``, ``_r{8hex}``) still fit within 63 bytes without their own truncation.
_SCHEMA_NAME_MAX_BYTES = 50


def sanitize_identifier(raw: str) -> str:
    """Reduce an arbitrary string to a safe lowercase PostgreSQL identifier body.

    Lowercases, maps ``-`` to ``_``, drops every other non-alphanumeric/underscore
    character, and prefixes ``t_`` when the result would start with a digit.
    Returns ``"unknown"`` if nothing survives. This is the historical
    ``_sanitize_schema_name`` contract, kept byte-identical so existing view names
    (which route through it) do not change — collision-safety comes from the
    digest in :func:`tenant_schema_name`, not from the sanitizer being injective.
    """
    name = raw.lower().replace("-", "_")
    name = "".join(c for c in name if c.isalnum() or c == "_")
    if name and name[0].isdigit():
        name = f"t_{name}"
    return name or "unknown"


def _digest(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:_DIGEST_LEN]


def _truncate_to_bytes(s: str, max_bytes: int) -> str:
    if max_bytes <= 0:
        return ""
    encoded = s.encode("utf-8")
    if len(encoded) <= max_bytes:
        return s
    # Identifier chars are ASCII after sanitization, but decode defensively on a
    # byte boundary so a multibyte char is never split.
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def fit_identifier(
    base: str,
    *,
    suffix: str = "",
    unique_key: str | None = None,
    max_bytes: int = PG_MAX_IDENTIFIER_BYTES,
    always_hash: bool = False,
) -> str:
    """Compose a valid PostgreSQL identifier of at most ``max_bytes`` bytes.

    ``base`` is sanitized; ``suffix`` (assumed already safe, e.g. ``"_ro"``) is
    appended verbatim. When ``always_hash`` is set, or when the plain form would
    exceed ``max_bytes``, an 8-char digest of ``unique_key`` (falling back to
    ``base``) is woven in before the suffix. The digest and suffix are always
    preserved; only the human-readable head is trimmed to fit.
    """
    head = sanitize_identifier(base)
    plain = f"{head}{suffix}"
    if not always_hash and len(plain.encode("utf-8")) <= max_bytes:
        return plain

    digest = _digest(unique_key if unique_key is not None else base)
    tail = f"_{digest}{suffix}"
    head = _truncate_to_bytes(head, max_bytes - len(tail.encode("utf-8"))).rstrip("_")
    if not head or head[0].isdigit():
        head = f"t{head}" if head else "t"
    return f"{head}{tail}"


def tenant_schema_name(provider: str, external_id: str) -> str:
    """Mint the schema name for a tenant, unique per ``(provider, external_id)``.

    Always carries the identity digest, so a cross-provider duplicate external_id
    or a punctuation/length collision can never route one tenant into another's
    physical schema.
    """
    return fit_identifier(
        external_id,
        unique_key=f"{provider}\x00{external_id}",
        max_bytes=_SCHEMA_NAME_MAX_BYTES,
        always_hash=True,
    )


def refresh_schema_name(provider: str, external_id: str, *, token: str) -> str:
    """Mint a unique schema name for one background refresh of a tenant.

    ``token`` (a short random hex string from the caller) makes the name unique
    per refresh; the identity digest keeps it tied to the tenant. The whole name
    stays within 63 bytes so its derived ``_ro``/``_dbt`` roles never truncate.
    """
    return fit_identifier(
        external_id,
        suffix=f"_r{token}",
        unique_key=f"{provider}\x00{external_id}",
        always_hash=True,
    )


def readonly_role_name(schema_name: str) -> str:
    """Derive the read-only role name for a schema (``{schema}_ro`` when it fits)."""
    return fit_identifier(schema_name, suffix="_ro", unique_key=schema_name)


def dbt_role_name(schema_name: str) -> str:
    """Derive the low-privilege dbt role name for a schema (issue #241).

    dbt assumes this role (via ``SET ROLE`` in its profile) when materializing
    transformation assets, so user-authored SQL runs with rights on this schema
    only — never as the full ``MANAGED_DATABASE_URL`` superuser.
    """
    return fit_identifier(schema_name, suffix="_dbt", unique_key=schema_name)


def dbt_model_name(name: str) -> str:
    """Guard a fully-composed dbt model name to <=63 bytes (cap+hash on overflow).

    The caller builds the readable name (``stg_case_<slug>`` etc.); this ensures
    two long names sharing a 63-byte prefix become distinct physical relations
    rather than one silently overwriting the other.
    """
    return fit_identifier(name, unique_key=name)


def dbt_column_alias(base: str, seen: dict[str, int]) -> str:
    """Return a unique dbt column alias, capped to 63 bytes.

    Duplicates are disambiguated (``base``, ``base_2``, ...) and only THEN passed
    through the byte guard — so distinct long property names cannot collapse to
    one physical column the way ``_unique_alias`` (which disambiguated *before*
    truncation) allowed.
    """
    if base in seen:
        seen[base] += 1
        unique = f"{base}_{seen[base]}"
    else:
        seen[base] = 1
        unique = base
    return fit_identifier(unique, unique_key=unique)
