"""read_attachment and the note for attachments pruned from the window (#913)."""

import uuid

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from apps.agents.graph.state import prune_messages
from apps.agents.graph.tool_binding import _build_tools
from apps.agents.tools import attachment_tool
from apps.agents.tools.attachment_tool import create_read_attachment_tool
from apps.chat.attachments import SCOUT_ATTACHMENTS_KWARG
from apps.chat.models import AttachmentBlob, ChatAttachment, Thread

TEXT = "".join(f"row {i}: value {i * 7}\n" for i in range(1000))


async def _attachment(user, workspace, thread, content: bytes, filename, media_type):
    blob = await AttachmentBlob.objects.acreate(
        sha256=uuid.uuid4().hex + uuid.uuid4().hex, size=len(content), content=content
    )
    return await ChatAttachment.objects.acreate(
        blob=blob,
        user=user,
        workspace=workspace,
        thread=thread,
        filename=filename,
        media_type=media_type,
    )


@pytest.fixture
def thread(user, workspace):
    return Thread.objects.create(user=user, workspace=workspace)


async def _read(thread, **args):
    return await create_read_attachment_tool(str(thread.id)).ainvoke(args)


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_reads_a_window_and_says_where_to_continue(user, workspace, thread):
    attachment = await _attachment(user, workspace, thread, TEXT.encode(), "rows.txt", "text/plain")

    result = await _read(thread, attachment_id=str(attachment.id), offset=10, limit=30)

    assert result.startswith(f"[rows.txt: characters 10-40 of {len(TEXT):,}]\n")
    assert TEXT[10:40] in result
    assert "offset=40" in result


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_caps_the_limit(user, workspace, thread, monkeypatch):
    monkeypatch.setattr(attachment_tool, "MAX_LIMIT", 5)
    attachment = await _attachment(user, workspace, thread, TEXT.encode(), "rows.txt", "text/plain")
    result = await _read(thread, attachment_id=str(attachment.id), limit=10_000)
    assert "characters 0-5 of" in result


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_search_returns_each_match_with_its_offset(user, workspace, thread):
    attachment = await _attachment(user, workspace, thread, TEXT.encode(), "rows.txt", "text/plain")

    result = await _read(thread, attachment_id=str(attachment.id), search="ROW 99:")

    offset = TEXT.index("row 99:")
    assert f"offset {offset} (line 100): …" in result
    assert result.count("\noffset ") == 1


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_search_finds_matches_inside_a_single_line_file(user, workspace, thread):
    minified = '{"items": [' + ",".join(f'{{"id": {i}}}' for i in range(2000)) + "]}"
    attachment = await _attachment(
        user, workspace, thread, minified.encode(), "m.json", "application/json"
    )

    result = await _read(thread, attachment_id=str(attachment.id), search='"id": 1500}')

    offset = minified.index('"id": 1500}')
    assert f"offset {offset} (line 1)" in result
    assert '"id": 1500}' in result


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_search_caps_matches(user, workspace, thread):
    attachment = await _attachment(user, workspace, thread, TEXT.encode(), "rows.txt", "text/plain")
    result = await _read(thread, attachment_id=str(attachment.id), search="row")
    assert result.count("\noffset ") == attachment_tool.MAX_SEARCH_MATCHES
    assert "950 more matches" in result


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_only_reads_this_threads_attachments(user, workspace, thread):
    other = await Thread.objects.acreate(user=user, workspace=workspace)
    foreign = await _attachment(user, workspace, other, b"secret", "s.txt", "text/plain")
    unsent = await _attachment(user, workspace, None, b"draft", "d.txt", "text/plain")

    for attachment in (foreign, unsent):
        result = await _read(thread, attachment_id=str(attachment.id))
        assert result.startswith("No attachment")
    assert (await _read(thread, attachment_id="not-a-uuid")).startswith("No attachment")


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_images_are_not_read_as_text(user, workspace, thread):
    image = await _attachment(
        user, workspace, thread, b"\x89PNG\r\n\x1a\n", "shot.png", "image/png"
    )
    assert "is an image" in await _read(thread, attachment_id=str(image.id))


@pytest.mark.django_db
def test_interactive_threads_get_the_tool(user, workspace):
    names = {t.name for t in _build_tools(workspace, user, [], conversation_id=str(uuid.uuid4()))}
    assert "read_attachment" in names
    headless = _build_tools(workspace, user, [], interactive=False)
    assert "read_attachment" not in {t.name for t in headless}


def _with(content, *entries):
    return HumanMessage(content=content, additional_kwargs={SCOUT_ATTACHMENTS_KWARG: list(entries)})


def test_pruning_leaves_a_note_for_dropped_attachments():
    text = {
        "id": "t1",
        "filename": "registry.json",
        "media_type": "application/json",
        "size": 92114,
    }
    image = {"id": "i1", "filename": "shot.png", "media_type": "image/png", "size": 10}
    history = [_with("first", text, image), AIMessage(content="seen")]
    history += [HumanMessage(content=f"q{i}") for i in range(20)]

    pruned = prune_messages([SystemMessage(content="sys"), *history], max_messages=20)

    assert isinstance(pruned[0], SystemMessage)
    note = pruned[1]
    assert isinstance(note, HumanMessage)
    assert "registry.json (id t1, 92,114 bytes)" in note.content
    assert "the image shot.png (id i1)" in note.content
    assert "read_attachment" in note.content
    assert pruned[2:] == history[2:]


def test_pruning_adds_no_note_without_dropped_attachments():
    kept = _with("recent", {"id": "t1", "filename": "a.txt", "media_type": "text/plain"})
    history = [HumanMessage(content="old"), AIMessage(content="a")]
    history += [HumanMessage(content=f"q{i}") for i in range(19)] + [kept]
    pruned = prune_messages(history, max_messages=20)
    assert pruned == history[2:]
