"""Tests for schema context injection into the agent system prompt.

The status story (loaded / in-progress / not-ready) is driven by the canonical
``derive_world_state`` read-model (arch #251); these tests patch it directly so
the prompt branching is exercised independently of the ORM, and the table-listing
path is exercised on top of an ``available`` world state.
"""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from apps.agents.graph.base import (
    _fetch_multi_tenant_schema_context,
    _fetch_schema_context,
)
from apps.workspaces.services.catalog import CatalogTable, TableDescription
from apps.workspaces.services.world_state import WorldState


def _ct(name, *, type="source", description="", row_count=None, materialized_at=None):
    return CatalogTable(
        name=name,
        type=type,
        logical_name=name,
        description=description,
        row_count=row_count,
        materialized_at=materialized_at,
        verified=True,
    )


def _world(
    status="available",
    in_progress=False,
    last_synced_at=None,
    last_error=None,
    is_multi_tenant=False,
):
    return WorldState(
        status=status,
        in_progress=in_progress,
        last_synced_at=last_synced_at,
        last_error=last_error,
        is_multi_tenant=is_multi_tenant,
    )


@pytest.fixture
def mock_workspace():
    return MagicMock()


@pytest.fixture
def mock_tenant():
    m = MagicMock()
    m.external_id = "test-domain"
    m.canonical_name = "Test Domain"
    m.provider = "commcare"
    return m


@pytest.fixture
def mock_user():
    return MagicMock()


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_fetch_schema_context_not_provisioned(mock_workspace, mock_tenant, mock_user):
    """Returns 'no data' block when the world state is not available."""
    with patch(
        "apps.agents.graph.base.derive_world_state",
        new=AsyncMock(return_value=_world(status="unavailable")),
    ):
        result = await _fetch_schema_context(mock_workspace, mock_tenant, mock_user)

    assert "No data has been loaded yet" in result
    assert "run_materialization" in result
    # Finding 02#6: the prompt must NOT instruct a `pipeline=` argument — the
    # run_materialization MCP tool has no pipeline parameter (routing moved into
    # materialize_workspace per-provider) and its LLM-facing schema is empty.
    # The multi-tenant branch already omits it; the single-tenant branch must too.
    assert "pipeline=" not in result
    assert 'pipeline="' not in result


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_fetch_schema_context_in_progress(mock_workspace, mock_tenant, mock_user):
    """Returns 'currently loading' block when a materialization is in progress.

    Divergence (4) fix (arch #251): this branch now keys off
    ``WorldState.in_progress`` (a run in ACTIVE_STATES) — under the old
    ``SchemaState.MATERIALIZING`` gate it could never fire.
    """
    with patch(
        "apps.agents.graph.base.derive_world_state",
        new=AsyncMock(return_value=_world(status="provisioning", in_progress=True)),
    ):
        result = await _fetch_schema_context(mock_workspace, mock_tenant, mock_user)

    assert "loading" in result.lower()
    assert "run_materialization" not in result


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_fetch_schema_context_active_compact(mock_workspace, mock_tenant, mock_user):
    """Returns compact table list (no columns) when full schema exceeds budget."""
    from apps.workspaces.models import SchemaState

    mock_ts = MagicMock()
    mock_ts.state = SchemaState.ACTIVE

    mock_tables = [
        _ct(
            "cases",
            description="CommCare cases",
            row_count=1000,
            materialized_at="2026-03-02T10:00:00",
        ),
        _ct(
            "forms",
            description="CommCare forms",
            row_count=500,
            materialized_at="2026-03-02T10:00:00",
        ),
    ]

    # Full schema text that exceeds 6000 chars
    big_column_text = "x" * 7000

    with (
        patch(
            "apps.agents.graph.base.derive_world_state",
            new=AsyncMock(return_value=_world(status="available")),
        ),
        patch("apps.agents.graph.base.TenantSchema") as MockTS,
        patch(
            "apps.agents.graph.base.list_catalog",
            new=AsyncMock(return_value=mock_tables),
        ),
        patch("apps.agents.graph.base._render_full_schema") as mock_full,
        patch(
            "apps.transformations.services.lineage.aget_terminal_assets",
            new=AsyncMock(return_value=[]),
        ),
    ):
        MockTS.objects.filter.return_value.afirst = AsyncMock(return_value=mock_ts)
        mock_full.return_value = big_column_text  # triggers fallback

        result = await _fetch_schema_context(mock_workspace, mock_tenant, mock_user)

    assert "cases" in result
    assert "forms" in result
    assert "1,000" in result or "1000" in result
    assert "describe_table" in result  # compact fallback note


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_fetch_schema_context_active_full(mock_workspace, mock_tenant, mock_user):
    """Returns full schema with columns when it fits within the 6000-char budget."""
    from apps.workspaces.models import SchemaState

    mock_ts = MagicMock()
    mock_ts.state = SchemaState.ACTIVE

    mock_tables = [
        _ct(
            "cases",
            description="CommCare cases",
            row_count=100,
            materialized_at="2026-03-02T10:00:00",
        ),
    ]

    small_column_text = (
        "**cases** — CommCare cases (100 rows)\nColumns:\n- case_id (text)\n- closed (boolean)\n"
    )

    with (
        patch(
            "apps.agents.graph.base.derive_world_state",
            new=AsyncMock(return_value=_world(status="available")),
        ),
        patch("apps.agents.graph.base.TenantSchema") as MockTS,
        patch(
            "apps.agents.graph.base.list_catalog",
            new=AsyncMock(return_value=mock_tables),
        ),
        patch(
            "apps.agents.graph.base.describe",
            new=AsyncMock(
                return_value=TableDescription(
                    name="cases",
                    description="CommCare cases",
                    columns=[{"name": "case_id", "type": "text"}],
                )
            ),
        ),
        patch("apps.agents.graph.base._render_full_schema") as mock_full,
        patch(
            "apps.agents.graph.base.load_tenant_context", new=AsyncMock(return_value=MagicMock())
        ),
        patch(
            "apps.agents.graph.base.aget_tenant_metadata", new=AsyncMock(return_value=None)
        ),
        patch(
            "apps.transformations.services.lineage.aget_terminal_assets",
            new=AsyncMock(return_value=[]),
        ),
    ):
        MockTS.objects.filter.return_value.afirst = AsyncMock(return_value=mock_ts)
        mock_full.return_value = small_column_text

        result = await _fetch_schema_context(mock_workspace, mock_tenant, mock_user)

    assert "case_id" in result or small_column_text in result
    assert "describe_table" not in result  # no fallback note in full tier


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_fetch_schema_context_no_get_schema_status_instruction(
    mock_workspace, mock_tenant, mock_user
):
    """The returned text must NOT instruct the agent to call get_schema_status."""
    from apps.workspaces.models import SchemaState

    mock_ts = MagicMock()
    mock_ts.state = SchemaState.ACTIVE

    with (
        patch(
            "apps.agents.graph.base.derive_world_state",
            new=AsyncMock(return_value=_world(status="available")),
        ),
        patch("apps.agents.graph.base.TenantSchema") as MockTS,
        patch(
            "apps.agents.graph.base.list_catalog",
            new=AsyncMock(return_value=[]),
        ),
        patch("apps.agents.graph.base._render_full_schema") as mock_full,
        patch(
            "apps.transformations.services.lineage.aget_terminal_assets",
            new=AsyncMock(return_value=[]),
        ),
    ):
        MockTS.objects.filter.return_value.afirst = AsyncMock(return_value=mock_ts)
        mock_full.return_value = ""

        result = await _fetch_schema_context(mock_workspace, mock_tenant, mock_user)

    assert "call `get_schema_status`" not in result
    assert "start of every conversation" not in result


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_build_system_prompt_no_schema_status_call():
    """The assembled system prompt must not instruct the agent to call get_schema_status."""
    from apps.agents.graph.base import _build_system_prompt
    from apps.workspaces.models import SchemaState

    mock_workspace = MagicMock()
    mock_workspace.system_prompt = None
    mock_workspace.tenants.acount = AsyncMock(return_value=1)

    mock_tenant = MagicMock()
    mock_tenant.external_id = "test-domain"
    mock_tenant.canonical_name = "Test"
    mock_tenant.provider = "commcare"
    mock_workspace.tenants.afirst = AsyncMock(return_value=mock_tenant)

    mock_ts = MagicMock()
    mock_ts.state = SchemaState.ACTIVE

    with (
        patch(
            "apps.agents.graph.base.derive_world_state",
            new=AsyncMock(return_value=_world(status="available")),
        ),
        patch("apps.agents.graph.base.KnowledgeRetriever") as MockKR,
        patch("apps.agents.graph.base.TenantSchema") as MockTS,
        patch(
            "apps.agents.graph.base.list_catalog",
            new=AsyncMock(return_value=[]),
        ),
        patch("apps.agents.graph.base._render_full_schema") as mock_full,
        patch(
            "apps.transformations.services.lineage.aget_terminal_assets",
            new=AsyncMock(return_value=[]),
        ),
    ):
        MockKR.return_value.retrieve = AsyncMock(return_value="")
        MockTS.objects.filter.return_value.afirst = AsyncMock(return_value=mock_ts)
        mock_full.return_value = ""

        # _build_system_prompt returns a (stable, volatile) split (arch #254).
        prompt = "\n".join(await _build_system_prompt(mock_workspace, MagicMock()))

    assert "call `get_schema_status`" not in prompt
    assert "start of every conversation" not in prompt
    assert "## Data Availability" in prompt


# --- Multi-tenant ---


class _AsyncIter:
    """Re-iterable async iterator over a fixed list, for mocking Django async QuerySets."""

    def __init__(self, items):
        self._items = list(items)

    def __aiter__(self):
        # Return a fresh iterator so `async for` can be used multiple times.
        return _AsyncIter(self._items).__aiter_inner__()

    async def __aiter_inner__(self):
        for item in self._items:
            yield item


@pytest.fixture
def mock_multi_workspace():
    """Workspace with 2 tenants. Subclass tunes `tenants` queryset behavior per test."""
    ws = MagicMock()
    ws.id = "11111111-1111-1111-1111-111111111111"
    t1 = MagicMock()
    t1.id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    t2 = MagicMock()
    t2.id = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    ws.tenants.all.return_value = _AsyncIter([t1, t2])
    return ws


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_fetch_multi_tenant_no_view_schema_no_runs(mock_multi_workspace, mock_user):
    """No view schema and no active runs -> agent told to call run_materialization."""
    with patch(
        "apps.agents.graph.base.derive_world_state",
        new=AsyncMock(return_value=_world(status="provisioning", is_multi_tenant=True)),
    ):
        result = await _fetch_multi_tenant_schema_context(mock_multi_workspace, mock_user)

    assert "No data has been loaded yet" in result
    assert "run_materialization" in result
    assert "multi-tenant" in result.lower()


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_fetch_multi_tenant_in_progress(mock_multi_workspace, mock_user):
    """A view-schema rebuild window or active run -> 'still loading' message."""
    with patch(
        "apps.agents.graph.base.derive_world_state",
        new=AsyncMock(
            return_value=_world(status="provisioning", in_progress=True, is_multi_tenant=True)
        ),
    ):
        result = await _fetch_multi_tenant_schema_context(mock_multi_workspace, mock_user)

    assert "in progress" in result.lower()
    assert "run_materialization" not in result


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_fetch_multi_tenant_active_with_tables(mock_multi_workspace, mock_user):
    """View schema available -> emits table list from workspace_list_tables + namespacing hint."""
    tables = [
        _ct("tenant_a__raw_cases", type="view"),
        _ct("tenant_a__raw_forms", type="view"),
        _ct("tenant_b__raw_cases", type="view"),
        _ct("tenant_b__raw_forms", type="view"),
    ]

    with (
        patch(
            "apps.agents.graph.base.derive_world_state",
            new=AsyncMock(
                return_value=_world(
                    status="available",
                    last_synced_at=datetime.fromisoformat("2026-05-22T10:00:00"),
                    is_multi_tenant=True,
                )
            ),
        ),
        patch(
            "apps.agents.graph.base.load_workspace_context",
            new=AsyncMock(return_value=MagicMock()),
        ),
        patch(
            "apps.agents.graph.base.list_catalog",
            new=AsyncMock(return_value=tables),
        ),
    ):
        result = await _fetch_multi_tenant_schema_context(mock_multi_workspace, mock_user)

    assert "Data is loaded and ready" in result
    assert "2026-05-22T10:00:00" in result
    assert "tenant_a__raw_cases" in result
    assert "tenant_b__raw_forms" in result
    assert "{tenant_name}__{table_name}" in result
    assert "describe_table" in result


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_build_system_prompt_multi_tenant_no_data_pre_fetched():
    """Multi-tenant workspace with no data: system prompt tells the agent upfront,
    NOT 'call list_tables to discover'."""
    from apps.agents.graph.base import _build_system_prompt

    ws = MagicMock()
    ws.id = "22222222-2222-2222-2222-222222222222"
    ws.system_prompt = None
    ws.tenants.acount = AsyncMock(return_value=2)
    ws.tenants.all.return_value = _AsyncIter([MagicMock(id="aa"), MagicMock(id="bb")])

    with (
        patch(
            "apps.agents.graph.base.derive_world_state",
            new=AsyncMock(return_value=_world(status="provisioning", is_multi_tenant=True)),
        ),
        patch("apps.agents.graph.base.KnowledgeRetriever") as MockKR,
    ):
        MockKR.return_value.retrieve = AsyncMock(return_value="")

        # _build_system_prompt returns a (stable, volatile) split (arch #254).
        prompt = "\n".join(await _build_system_prompt(ws, MagicMock()))

    assert "## Data Availability" in prompt
    assert "No data has been loaded yet" in prompt
    assert "run_materialization" in prompt
    # The old "just call list_tables to discover" hint must not be the only signal
    # — that was the bug. The agent should know up front there is no data.
    assert "Call `list_tables` to see all available tables." not in prompt
