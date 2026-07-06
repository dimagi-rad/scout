"""Tests for the shared async send_email Procrastinate task."""

import pytest
from django.core import mail

from apps.users.tasks import send_email
from config.settings import production


def test_production_ses_config_specifies_region():
    """SCOUT-DJANGO-22: the EC2 instance role gives boto3 credentials but no
    region, so ANYMAIL must set region_name or every send raises NoRegionError.
    Dev/test use the console/locmem backend, so only prod exercises the client.
    """
    assert production.ANYMAIL["AMAZON_SES_CLIENT_PARAMS"]["region_name"]


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_send_email_delivers_to_outbox():
    await send_email(
        subject="Hello",
        message="Body text",
        recipient_list=["invitee@example.com"],
    )
    assert len(mail.outbox) == 1
    sent = mail.outbox[0]
    assert sent.subject == "Hello"
    assert sent.body == "Body text"
    assert sent.to == ["invitee@example.com"]


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_send_email_uses_resilient_task_wrapper():
    """Registered through config.procrastinate.task so it survives dead DB conns."""
    assert getattr(send_email.func, "_ensures_fresh_db_connections", False) is True
