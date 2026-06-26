"""Tests for schema context injection into the agent system prompt."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from apps.agents.graph.base import (
    _fetch_multi_tenant_schema_context,
    _fetch_schema_context,
)


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
async def test_fetch_schema_context_not_provisioned(mock_tenant, mock_user):
    """Returns 'no data' block when TenantSchema does not exist."""
    with patch("apps.agents.graph.base.TenantSchema") as MockTS:
        MockTS.objects.filter.return_value.afirst = AsyncMock(return_value=None)
        result = await _fetch_schema_context(mock_tenant, mock_user)

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
async def test_fetch_schema_context_materializing(mock_tenant, mock_user):
    """Returns 'currently loading' block when schema state is materializing."""
    from apps.workspaces.models import SchemaState

    mock_ts = MagicMock()
    mock_ts.state = SchemaState.MATERIALIZING

    with patch("apps.agents.graph.base.TenantSchema") as MockTS:
        MockTS.objects.filter.return_value.afirst = AsyncMock(return_value=mock_ts)
        result = await _fetch_schema_context(mock_tenant, mock_user)

    assert "loading" in result.lower()
    assert "run_materialization" not in result


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_fetch_schema_context_active_compact(mock_tenant, mock_user):
    """Returns compact table list (no columns) when full schema exceeds budget."""
    from apps.workspaces.models import SchemaState

    mock_ts = MagicMock()
    mock_ts.state = SchemaState.ACTIVE

    mock_tables = [
        {
            "name": "cases",
            "description": "CommCare cases",
            "materialized_row_count": 1000,
            "row_count_verified": False,
            "materialized_at": "2026-03-02T10:00:00",
        },
        {
            "name": "forms",
            "description": "CommCare forms",
            "materialized_row_count": 500,
            "row_count_verified": False,
            "materialized_at": "2026-03-02T10:00:00",
        },
    ]

    # Full schema text that exceeds 6000 chars
    big_column_text = "x" * 7000

    with (
        patch("apps.agents.graph.base.TenantSchema") as MockTS,
        patch("apps.agents.graph.base.get_registry") as mock_registry,
        patch(
            "apps.agents.graph.base.pipeline_list_tables",
            new=AsyncMock(return_value=mock_tables),
        ),
        patch("apps.agents.graph.base._render_full_schema") as mock_full,
        patch(
            "apps.transformations.services.lineage.aget_terminal_assets",
            new=AsyncMock(return_value=[]),
        ),
    ):
        MockTS.objects.filter.return_value.afirst = AsyncMock(return_value=mock_ts)
        mock_registry.return_value.get.return_value = MagicMock()
        mock_full.return_value = big_column_text  # triggers fallback

        result = await _fetch_schema_context(mock_tenant, mock_user)

    assert "cases" in result
    assert "forms" in result
    assert "1,000" in result or "1000" in result
    assert "describe_table" in result  # compact fallback note


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_fetch_schema_context_active_full(mock_tenant, mock_user):
    """Returns full schema with columns when it fits within the 6000-char budget."""
    from apps.workspaces.models import SchemaState

    mock_ts = MagicMock()
    mock_ts.state = SchemaState.ACTIVE

    mock_tables = [
        {
            "name": "cases",
            "description": "CommCare cases",
            "materialized_row_count": 100,
            "row_count_verified": False,
            "materialized_at": "2026-03-02T10:00:00",
        },
    ]

    small_column_text = (
        "**cases** — CommCare cases (100 rows)\nColumns:\n- case_id (text)\n- closed (boolean)\n"
    )

    with (
        patch("apps.agents.graph.base.TenantSchema") as MockTS,
        patch("apps.agents.graph.base.get_registry") as mock_registry,
        patch(
            "apps.agents.graph.base.pipeline_list_tables",
            new=AsyncMock(return_value=mock_tables),
        ),
        patch(
            "apps.agents.graph.base.pipeline_describe_table",
            new=AsyncMock(return_value={"columns": [{"name": "case_id", "type": "text"}]}),
        ),
        patch("apps.agents.graph.base._render_full_schema") as mock_full,
        patch(
            "apps.agents.graph.base.load_tenant_context", new=AsyncMock(return_value=MagicMock())
        ),
        patch("apps.workspaces.models.TenantMetadata") as MockTM,
        patch(
            "apps.transformations.services.lineage.aget_terminal_assets",
            new=AsyncMock(return_value=[]),
        ),
    ):
        MockTS.objects.filter.return_value.afirst = AsyncMock(return_value=mock_ts)
        mock_registry.return_value.get.return_value = MagicMock()
        mock_full.return_value = small_column_text
        MockTM.objects.filter.return_value.afirst = AsyncMock(return_value=None)

        result = await _fetch_schema_context(mock_tenant, mock_user)

    assert "case_id" in result or small_column_text in result
    assert "describe_table" not in result  # no fallback note in full tier


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_fetch_schema_context_no_get_schema_status_instruction(mock_tenant, mock_user):
    """The returned text must NOT instruct the agent to call get_schema_status."""
    from apps.workspaces.models import SchemaState

    mock_ts = MagicMock()
    mock_ts.state = SchemaState.ACTIVE

    with (
        patch("apps.agents.graph.base.TenantSchema") as MockTS,
        patch("apps.agents.graph.base.get_registry") as mock_registry,
        patch(
            "apps.agents.graph.base.pipeline_list_tables",
            new=AsyncMock(return_value=[]),
        ),
        patch("apps.agents.graph.base._render_full_schema") as mock_full,
        patch(
            "apps.transformations.services.lineage.aget_terminal_assets",
            new=AsyncMock(return_value=[]),
        ),
    ):
        MockTS.objects.filter.return_value.afirst = AsyncMock(return_value=mock_ts)
        mock_registry.return_value.get.return_value = MagicMock()
        mock_full.return_value = ""

        result = await _fetch_schema_context(mock_tenant, mock_user)

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
        patch("apps.agents.graph.base.KnowledgeRetriever") as MockKR,
        patch("apps.agents.graph.base.TenantSchema") as MockTS,
        patch("apps.agents.graph.base.get_registry") as mock_reg,
        patch(
            "apps.agents.graph.base.pipeline_list_tables",
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
        mock_reg.return_value.get.return_value = MagicMock()
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
    with (
        patch("apps.agents.graph.base.WorkspaceViewSchema") as MockVS,
        patch("apps.agents.graph.base.MaterializationRun") as MockMR,
    ):
        MockVS.objects.filter.return_value.afirst = AsyncMock(return_value=None)
        MockMR.objects.filter.return_value.afirst = AsyncMock(return_value=None)
        MockMR.ACTIVE_STATES = frozenset({"started", "discovering", "loading", "transforming"})

        result = await _fetch_multi_tenant_schema_context(mock_multi_workspace, mock_user)

    assert "No data has been loaded yet" in result
    assert "run_materialization" in result
    assert "multi-tenant" in result.lower()


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_fetch_multi_tenant_view_schema_materializing(mock_multi_workspace, mock_user):
    """View schema in MATERIALIZING -> 'still loading' message."""
    from apps.workspaces.models import SchemaState

    vs = MagicMock()
    vs.state = SchemaState.MATERIALIZING

    with (
        patch("apps.agents.graph.base.WorkspaceViewSchema") as MockVS,
        patch("apps.agents.graph.base.MaterializationRun") as MockMR,
    ):
        MockVS.objects.filter.return_value.afirst = AsyncMock(return_value=vs)
        MockMR.objects.filter.return_value.afirst = AsyncMock(return_value=None)
        MockMR.ACTIVE_STATES = frozenset({"started", "discovering", "loading", "transforming"})

        result = await _fetch_multi_tenant_schema_context(mock_multi_workspace, mock_user)

    assert "in progress" in result.lower()
    assert "run_materialization" not in result


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_fetch_multi_tenant_active_materialization_run(mock_multi_workspace, mock_user):
    """An active MaterializationRun for a tenant -> 'still loading' message, even if
    the view schema is not present yet."""
    active_run = MagicMock()

    with (
        patch("apps.agents.graph.base.WorkspaceViewSchema") as MockVS,
        patch("apps.agents.graph.base.MaterializationRun") as MockMR,
    ):
        MockVS.objects.filter.return_value.afirst = AsyncMock(return_value=None)
        MockMR.objects.filter.return_value.afirst = AsyncMock(return_value=active_run)
        MockMR.ACTIVE_STATES = frozenset({"started", "discovering", "loading", "transforming"})

        result = await _fetch_multi_tenant_schema_context(mock_multi_workspace, mock_user)

    assert "in progress" in result.lower()
    assert "run_materialization" not in result


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_fetch_multi_tenant_active_with_tables(mock_multi_workspace, mock_user):
    """View schema ACTIVE -> emits table list from workspace_list_tables + namespacing hint."""
    from apps.workspaces.models import SchemaState

    vs = MagicMock()
    vs.state = SchemaState.ACTIVE

    completed_run = MagicMock()
    completed_run.completed_at.isoformat.return_value = "2026-05-22T10:00:00"

    tables = [
        {
            "name": "tenant_a__raw_cases",
            "type": "view",
            "materialized_row_count": None,
            "row_count_verified": False,
        },
        {
            "name": "tenant_a__raw_forms",
            "type": "view",
            "materialized_row_count": None,
            "row_count_verified": False,
        },
        {
            "name": "tenant_b__raw_cases",
            "type": "view",
            "materialized_row_count": None,
            "row_count_verified": False,
        },
        {
            "name": "tenant_b__raw_forms",
            "type": "view",
            "materialized_row_count": None,
            "row_count_verified": False,
        },
    ]

    with (
        patch("apps.agents.graph.base.WorkspaceViewSchema") as MockVS,
        patch("apps.agents.graph.base.MaterializationRun") as MockMR,
        patch(
            "apps.agents.graph.base.load_workspace_context",
            new=AsyncMock(return_value=MagicMock()),
        ),
        patch(
            "apps.agents.graph.base.workspace_list_tables",
            new=AsyncMock(return_value=tables),
        ),
    ):
        MockVS.objects.filter.return_value.afirst = AsyncMock(return_value=vs)
        MockMR.ACTIVE_STATES = frozenset({"started", "discovering", "loading", "transforming"})
        # filter(...).afirst() resolves the active-run check (None);
        # filter(...).order_by(...).afirst() resolves the last-completed-run lookup.
        MockMR.objects.filter.return_value.afirst = AsyncMock(return_value=None)
        MockMR.objects.filter.return_value.order_by.return_value.afirst = AsyncMock(
            return_value=completed_run
        )

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
        patch("apps.agents.graph.base.KnowledgeRetriever") as MockKR,
        patch("apps.agents.graph.base.WorkspaceViewSchema") as MockVS,
        patch("apps.agents.graph.base.MaterializationRun") as MockMR,
    ):
        MockKR.return_value.retrieve = AsyncMock(return_value="")
        MockVS.objects.filter.return_value.afirst = AsyncMock(return_value=None)
        MockMR.objects.filter.return_value.afirst = AsyncMock(return_value=None)
        MockMR.ACTIVE_STATES = frozenset({"started", "discovering", "loading", "transforming"})

        # _build_system_prompt returns a (stable, volatile) split (arch #254).
        prompt = "\n".join(await _build_system_prompt(ws, MagicMock()))

    assert "## Data Availability" in prompt
    assert "No data has been loaded yet" in prompt
    assert "run_materialization" in prompt
    # The old "just call list_tables to discover" hint must not be the only signal
    # — that was the bug. The agent should know up front there is no data.
    assert "Call `list_tables` to see all available tables." not in prompt
