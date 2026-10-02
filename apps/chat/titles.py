"""Thread titles, stored on the Thread row so listing threads never reads a checkpoint."""

from apps.chat.models import Thread

THREAD_TITLE_PREVIEW_CHARS = 200
UNTITLED = "Untitled"


def short_thread_title(title: str) -> str:
    clean = title.strip()
    if len(clean) > THREAD_TITLE_PREVIEW_CHARS:
        return f"{clean[:THREAD_TITLE_PREVIEW_CHARS].rstrip()}..."
    return clean


def display_thread_title(thread: Thread) -> str:
    return short_thread_title(thread.title) or UNTITLED
