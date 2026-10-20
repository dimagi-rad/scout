"""Workspace reference documents (#913): upload decoding, prompt listing and the read tool."""

import json

import pytest

from apps.agents.graph.tool_binding import _build_tools
from apps.agents.tools.reference_document_tool import create_reference_document_tool
from apps.knowledge.models import KnowledgeEntry
from apps.knowledge.services.reference_documents import (
    MAX_JSON_RECORD_CHARS,
    MAX_PROMPT_LISTED_DOCUMENTS,
    MAX_UPLOAD_BYTES,
    REFERENCE_DOCUMENTS_HEADING,
    ReferenceDocumentError,
    decode_upload,
)
from apps.knowledge.services.retriever import KNOWLEDGE_CONTEXT_CHAR_BUDGET, KnowledgeRetriever
from apps.workspaces.models import Workspace

REGISTRY = [
    {
        "code": f"IND{index:03d}",
        "name": f"Indicator {index}",
        "definition": "Share of visits where the child was weighed"
        if index == 7
        else f"Definition number {index} " + "x" * 200,
    }
    for index in range(400)
]
REGISTRY_TEXT = json.dumps(REGISTRY, indent=2)


class TestDecodeUpload:
    def test_json_gets_title_from_filename_and_a_structural_description(self):
        decoded = decode_upload("indicator-registry.json", REGISTRY_TEXT.encode())
        assert decoded.title == "indicator-registry"
        assert decoded.source_format == "json"
        assert decoded.description.startswith("JSON array of 400 entries")
        assert "code, name, definition" in decoded.description

    def test_strips_utf8_bom(self):
        decoded = decode_upload("codes.csv", "\ufeffcode,name\nA,Alpha\n".encode())
        assert decoded.content.startswith("code,name")

    def test_summary_of_long_keys_fits_the_description_column(self):
        record = {f"field_{index}_{'x' * 80}": index for index in range(8)}
        decoded = decode_upload("wide.json", json.dumps([record]).encode())
        assert len(decoded.description) <= 500

    @pytest.mark.parametrize(
        ("name", "data", "message"),
        [
            ("registry.xlsx", b"PK", "Unsupported file type"),
            ("registry.json", b"\xff\xfe\x00", "not valid UTF-8"),
            ("notes.txt", b"a\x00b", "binary data"),
            ("notes.txt", b"  \n", "empty"),
            ("registry.json", b'{"a": 1,}', "not valid JSON"),
            ("big.txt", b"a" * (MAX_UPLOAD_BYTES + 1), "too large"),
            ("deep.json", b"[" * 100_000 + b"]" * 100_000, "nested too deeply"),
        ],
    )
    def test_rejects(self, name, data, message):
        with pytest.raises(ReferenceDocumentError, match=message):
            decode_upload(name, data)


@pytest.mark.django_db(transaction=True)
class TestPromptListing:
    @pytest.mark.asyncio
    async def test_reference_docs_are_listed_not_injected(self, workspace):
        await KnowledgeEntry.objects.acreate(
            workspace=workspace, title="Active user", content="Logged a visit in 30 days."
        )
        await KnowledgeEntry.objects.acreate(
            workspace=workspace,
            title="Indicator registry",
            content=REGISTRY_TEXT,
            kind=KnowledgeEntry.Kind.REFERENCE,
            source_format="json",
            description="Official data elements and indicators",
        )

        result = await KnowledgeRetriever(workspace).retrieve()

        assert "Logged a visit in 30 days." in result
        assert "IND007" not in result
        assert REFERENCE_DOCUMENTS_HEADING in result
        assert (
            f'- "Indicator registry" (JSON, {len(REGISTRY_TEXT):,} chars): '
            "Official data elements and indicators"
        ) in result
        assert "read_reference_document" in result
        # A 400-entry registry no longer pushes the notes over budget.
        assert "truncated" not in result

    @pytest.mark.asyncio
    async def test_long_titles_are_cut_to_a_resolvable_prefix(self, workspace):
        await KnowledgeEntry.objects.acreate(
            workspace=workspace, title="R" * 250, content="x", kind="reference"
        )
        result = await KnowledgeRetriever(workspace).retrieve()
        assert f'- "{"R" * 100}" (TEXT' in result

        tool = create_reference_document_tool(workspace)
        read = await tool.ainvoke({"title_or_id": "R" * 100})
        assert read["status"] == "ok"

    @pytest.mark.asyncio
    async def test_listing_falls_back_to_first_meaningful_line(self, workspace):
        await KnowledgeEntry.objects.acreate(
            workspace=workspace,
            title="Codes",
            content="\n\ncode,name\nA,Alpha\n",
            kind=KnowledgeEntry.Kind.REFERENCE,
            source_format="csv",
        )
        result = await KnowledgeRetriever(workspace).retrieve()
        assert '- "Codes" (CSV, 20 chars): code,name' in result

    @pytest.mark.asyncio
    async def test_listing_is_capped_and_stable(self, workspace):
        for index in range(MAX_PROMPT_LISTED_DOCUMENTS + 3):
            await KnowledgeEntry.objects.acreate(
                workspace=workspace,
                title=f"Doc {index:02d}",
                content="x",
                kind=KnowledgeEntry.Kind.REFERENCE,
            )
        first = await KnowledgeRetriever(workspace).retrieve()
        second = await KnowledgeRetriever(workspace).retrieve()
        assert first == second
        assert first.count('- "Doc ') == MAX_PROMPT_LISTED_DOCUMENTS
        assert "… and 3 more" in first

    @pytest.mark.asyncio
    async def test_notes_still_honour_the_budget(self, workspace):
        await KnowledgeEntry.objects.acreate(
            workspace=workspace, title="Huge note", content="word " * 5000
        )
        await KnowledgeEntry.objects.acreate(
            workspace=workspace, title="Ref", content="x", kind=KnowledgeEntry.Kind.REFERENCE
        )
        result = await KnowledgeRetriever(workspace).retrieve()
        knowledge, _, listing = result.partition(REFERENCE_DOCUMENTS_HEADING)
        assert len(knowledge.rstrip()) <= KNOWLEDGE_CONTEXT_CHAR_BUDGET
        assert '- "Ref"' in listing


@pytest.mark.django_db(transaction=True)
class TestReadReferenceDocumentTool:
    @pytest.mark.asyncio
    async def test_pages_through_a_document(self, workspace):
        doc = await KnowledgeEntry.objects.acreate(
            workspace=workspace,
            title="Indicator registry",
            content=REGISTRY_TEXT,
            kind=KnowledgeEntry.Kind.REFERENCE,
            source_format="json",
        )
        tool = create_reference_document_tool(workspace)

        first = await tool.ainvoke({"title_or_id": "indicator registry", "limit": 1000})
        assert first["id"] == str(doc.id)
        assert first["content"] == REGISTRY_TEXT[:1000]
        assert first["total_chars"] == len(REGISTRY_TEXT)
        assert first["next_offset"] == 1000

        last = await tool.ainvoke(
            {"title_or_id": str(doc.id), "offset": len(REGISTRY_TEXT) - 10, "limit": 1000}
        )
        assert last["content"] == REGISTRY_TEXT[-10:]
        assert last["next_offset"] is None

    @pytest.mark.asyncio
    async def test_json_search_returns_the_matching_entries_with_paths(self, workspace):
        await KnowledgeEntry.objects.acreate(
            workspace=workspace,
            title="Indicator registry",
            content=REGISTRY_TEXT,
            kind=KnowledgeEntry.Kind.REFERENCE,
            source_format="json",
        )
        tool = create_reference_document_tool(workspace)

        result = await tool.ainvoke({"title_or_id": "registry", "search": "child WEIGHED"})

        assert result["match_mode"] == "json_entries"
        assert result["total_matches"] == 1
        assert result["matches"] == [{"path": "$[7]", "entry": REGISTRY[7]}]

    @pytest.mark.asyncio
    async def test_json_search_matches_object_keys(self, workspace):
        registry = {"indicators": {f"IND{i:03d}": {"name": "n" * 50} for i in range(200)}}
        await KnowledgeEntry.objects.acreate(
            workspace=workspace,
            title="Keyed",
            content=json.dumps(registry),
            kind=KnowledgeEntry.Kind.REFERENCE,
            source_format="json",
        )
        assert len(json.dumps(registry)) > MAX_JSON_RECORD_CHARS
        tool = create_reference_document_tool(workspace)

        result = await tool.ainvoke({"title_or_id": "Keyed", "search": "IND042"})

        assert result["matches"] == [{"path": "$.indicators.IND042", "entry": {"name": "n" * 50}}]

    @pytest.mark.asyncio
    async def test_line_search_keeps_csv_header_and_does_not_redact_sql(self, workspace):
        content = "code,rule\n" + "\n".join(
            [f"C{i},plain row {i}" for i in range(20)]
            + ["C99,SELECT count(*) FROM visits WHERE weighed"]
        )
        await KnowledgeEntry.objects.acreate(
            workspace=workspace,
            title="Rules",
            content=content,
            kind=KnowledgeEntry.Kind.REFERENCE,
            source_format="csv",
        )
        tool = create_reference_document_tool(workspace)

        result = await tool.ainvoke({"title_or_id": "Rules", "search": "weighed"})

        assert result["match_mode"] == "lines"
        assert result["header"] == "code,rule"
        [match] = result["matches"]
        assert match["lines"] == "20-22"
        assert ">22: C99,SELECT count(*) FROM visits WHERE weighed" in match["text"]
        assert " 20: C18,plain row 18" in match["text"]

    @pytest.mark.asyncio
    async def test_a_term_on_every_line_stays_within_the_limit(self, workspace):
        content = "\n".join(f"row {index}, value {'y' * 100}" for index in range(2000))
        await KnowledgeEntry.objects.acreate(
            workspace=workspace, title="Rows", content=content, kind="reference"
        )
        tool = create_reference_document_tool(workspace)

        result = await tool.ainvoke({"title_or_id": "Rows", "search": "row", "limit": 5000})

        # Counted in matching lines, not in the context windows that group them.
        assert result["total_matches"] == 2000
        shown = sum(match["text"].count(">") for match in result["matches"])
        assert result["note"].startswith(f"Showing {shown} of 2000")
        assert all(match["text"].count("\n") < 25 for match in result["matches"])
        assert len(json.dumps(result["matches"])) < 6000

    @pytest.mark.asyncio
    async def test_a_single_oversized_match_is_clipped_to_the_limit(self, workspace):
        await KnowledgeEntry.objects.acreate(
            workspace=workspace,
            title="Blob",
            content=json.dumps({"note": "needle " + "z" * 10_000}),
            kind="reference",
            source_format="json",
        )
        tool = create_reference_document_tool(workspace)

        result = await tool.ainvoke({"title_or_id": "Blob", "search": "needle", "limit": 1000})

        [match] = result["matches"]
        assert match["path"] == "$.note"
        assert len(match["entry"]) <= 1000
        assert "cut to fit the limit" in result["note"]

    @pytest.mark.asyncio
    async def test_search_output_respects_limit(self, workspace):
        await KnowledgeEntry.objects.acreate(
            workspace=workspace,
            title="Indicator registry",
            content=REGISTRY_TEXT,
            kind=KnowledgeEntry.Kind.REFERENCE,
            source_format="json",
        )
        tool = create_reference_document_tool(workspace)

        result = await tool.ainvoke(
            {"title_or_id": "registry", "search": "definition number", "limit": 1000}
        )

        assert result["total_matches"] == 399
        assert 1 <= len(result["matches"]) < 10
        assert "Showing" in result["note"]

    @pytest.mark.asyncio
    async def test_scoped_to_the_workspace_and_to_reference_docs(self, workspace, user):
        other = await Workspace.objects.acreate(name="Other", created_by=user)
        await KnowledgeEntry.objects.acreate(
            workspace=other, title="Secret registry", content="x", kind="reference"
        )
        await KnowledgeEntry.objects.acreate(
            workspace=workspace, title="Secret note", content="a note"
        )
        await KnowledgeEntry.objects.acreate(
            workspace=workspace, title="Codes", content="x", kind="reference"
        )
        tool = create_reference_document_tool(workspace)

        result = await tool.ainvoke({"title_or_id": "Secret"})

        assert result["status"] == "error"
        assert result["documents"] == [
            {"id": result["documents"][0]["id"], "title": "Codes", "total_chars": 1}
        ]

    @pytest.mark.asyncio
    async def test_blank_search_is_an_error_not_a_page(self, workspace):
        await KnowledgeEntry.objects.acreate(
            workspace=workspace, title="Codes", content="code\nA", kind="reference"
        )
        tool = create_reference_document_tool(workspace)

        result = await tool.ainvoke({"title_or_id": "Codes", "search": "   "})

        assert result == {"status": "error", "message": "search must contain at least one word."}

    @pytest.mark.asyncio
    async def test_ambiguous_title_lists_candidates(self, workspace):
        for title in ("Registry A", "Registry B"):
            await KnowledgeEntry.objects.acreate(
                workspace=workspace, title=title, content="x", kind="reference"
            )
        tool = create_reference_document_tool(workspace)

        result = await tool.ainvoke({"title_or_id": "registry"})

        assert result["status"] == "error"
        assert [doc["title"] for doc in result["documents"]] == ["Registry A", "Registry B"]

    def test_bound_for_every_role_and_mode(self, workspace, user):
        for interactive in (True, False):
            for write_capable in (True, False):
                tools = _build_tools(
                    workspace,
                    user,
                    [],
                    interactive=interactive,
                    write_capable=write_capable,
                )
                assert "read_reference_document" in {tool.name for tool in tools}
