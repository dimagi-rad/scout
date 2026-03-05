"""Shared utility for looking up CommCare OAuth credentials."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apps.users.models import User

logger = logging.getLogger(__name__)


def get_commcare_credential(user: User) -> dict[str, str] | None:
    """Look up the user's CommCare OAuth token if they have one.

    Returns {"type": "oauth", "value": "<token>"} or None.
    """
    try:
        from allauth.socialaccount.models import SocialToken

        token = (
            SocialToken.objects.filter(
                account__user=user,
                account__provider__startswith="commcare",
            )
            .exclude(account__provider__startswith="commcare_connect")
            .first()
        )
        if token and token.token:
            logger.info(
                "Found CommCare OAuth token for user %s (provider=%s)",
                user,
                token.account.provider,
            )
            return {"type": "oauth", "value": token.token}
        logger.info("No CommCare OAuth token found for user %s", user)
    except Exception:
        logger.debug("Could not look up CommCare credential for user", exc_info=True)
    return None
