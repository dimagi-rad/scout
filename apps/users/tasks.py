"""Shared async background tasks for the users app."""

from asgiref.sync import sync_to_async
from django.conf import settings
from django.core.mail import send_mail

from config.procrastinate import task


@task
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
