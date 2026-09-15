"""Unwrap CommCare's localized ``{"en": "Name"}`` display values."""

from __future__ import annotations


def localized_str(value: object) -> str:
    """Return the plain string behind a possibly-multilingual CommCare value.

    CommCare returns some display fields (app, module, form names; module case
    types) as translation dicts. Prefer English, then the first translation
    that unwraps to a non-empty string. Anything that is not a string after
    unwrapping degrades to ``""`` so callers fall through to their own default
    or digest fallback instead of raising mid-way through a tenant.
    """
    if isinstance(value, dict):
        for candidate in (value.get("en"), *value.values()):
            if text := localized_str(candidate):
                return text
        return ""
    return value if isinstance(value, str) else ""
