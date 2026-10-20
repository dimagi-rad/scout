"""Shared async background tasks for the users app."""

import logging

from asgiref.sync import sync_to_async
from django.conf import settings
from django.core.cache import cache
from django.core.mail import send_mail

from apps.users.services.source_descriptions import aprobe_connection_chatbots
from config.procrastinate import app

logger = logging.getLogger(__name__)

# Descriptions change rarely; re-probing every chatbot on each discovery would
# spend the token's OCS rate limit that its data loads also draw on.
OCS_PROBE_COOLDOWN_SECONDS = 6 * 3600


@app.task
async def send_email(subject, message, recipient_list, from_email=None, html_message=None):
    """Deliver a transactional email off the request path.

    Mirrors Connect's ``send_mail_async``: a thin wrapper over Django's
    ``send_mail`` so the backend (console in dev, Amazon SES in prod) is a
    settings concern. ``send_mail`` is blocking network I/O, so it runs in a
    thread rather than on the async worker loop.
    """
    await sync_to_async(send_mail)(
        subject=subject,
        message=message,
        from_email=from_email or settings.DEFAULT_FROM_EMAIL,
        recipient_list=recipient_list,
        html_message=html_message,
        fail_silently=False,
    )


@app.task
async def probe_ocs_chatbots(connection_id: str) -> None:
    """Record each chatbot's description for one OCS connection."""
    base_url = getattr(settings, "OCS_URL", "https://www.openchatstudio.com")
    await aprobe_connection_chatbots(connection_id, base_url)


async def aschedule_ocs_probe(connection_id) -> None:
    """Queue the chatbot probe for a connection, at most once per cooldown.

    Never raises: descriptions only decorate the picker and the prompt, so a
    queue failure must not fail the discovery that triggered it.
    """
    try:
        key = f"ocs_chatbot_probe:{connection_id}"
        if not await cache.aadd(key, True, OCS_PROBE_COOLDOWN_SECONDS):
            return
        try:
            await probe_ocs_chatbots.defer_async(connection_id=str(connection_id))
        except Exception:
            # Not queued, so do not hold the cooldown against the next discovery.
            await cache.adelete(key)
            raise
    except Exception:
        logger.warning("Could not queue the OCS chatbot probe", exc_info=True)
