"""Tests for the shared async send_email Procrastinate task."""

import pytest
from django.core import mail

from apps.users.tasks import send_email


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
