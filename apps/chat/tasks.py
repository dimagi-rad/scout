"""Background tasks for the chat app."""

import logging

from procrastinate.exceptions import AlreadyEnqueued

from apps.chat.models import Thread
from apps.chat.titles import agenerate_thread_title
from config.procrastinate import app

logger = logging.getLogger(__name__)

TITLE_JOB_PRIORITY = 10


def _title_lock(thread_id: str) -> str:
    return f"thread-title-{thread_id}"


@app.task
async def generate_thread_title(thread_id: str) -> str:
    return await agenerate_thread_title(thread_id)


async def aschedule_thread_title(thread: Thread) -> None:
    """Queue title generation after a successful turn, unless the title is settled.

    Best effort: a queueing failure must not fail the turn that triggered it.
    """
    if thread.title_source != Thread.TitleSource.FIRST_MESSAGE or thread.title_is_custom:
        return
    try:
        # Ahead of queued materializations, so the open page sees the title soon.
        await generate_thread_title.configure(
            queueing_lock=_title_lock(str(thread.id)), priority=TITLE_JOB_PRIORITY
        ).defer_async(thread_id=str(thread.id))
    except AlreadyEnqueued:
        pass
    except Exception:
        logger.warning("Could not queue title generation for thread %s", thread.id, exc_info=True)
