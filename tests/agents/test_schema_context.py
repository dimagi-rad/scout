"""Tests for schema context injection into the agent system prompt."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from apps.agents.graph.base import _fetch_schema_context


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


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_fetch_schema_context_materializing(mock_tenant, mock_user):
    """Returns 'currently loading' block when schema state is materializing."""
    from apps.projects.models import SchemaState

    mock_ts = MagicMock()
    mock_ts.state = SchemaState.MATERIALIZING

    with patch("apps.agents.graph.base.TenantSchema") as MockTS:
        MockTS.objects.filter.return_value.afirst = AsyncMock(return_value=mock_ts)
        result = await _fetch_schema_context(mock_tenant, mock_user)

    assert "currently loading" in result.lower()
    assert "run_materialization" not in result


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_fetch_schema_context_active_compact(mock_tenant, mock_user):
    """Returns compact table list (no columns) when full schema exceeds budget."""
    from apps.projects.models import SchemaState

    mock_ts = MagicMock()
    mock_ts.state = SchemaState.ACTIVE

    mock_tables = [
        {
            "name": "cases",
            "description": "CommCare cases",
            "row_count": 1000,
            "materialized_at": "2026-03-02T10:00:00",
        },
        {
            "name": "forms",
            "description": "CommCare forms",
            "row_count": 500,
            "materialized_at": "2026-03-02T10:00:00",
        },
    ]

    # Full schema text that exceeds 6000 chars
    big_column_text = "x" * 7000

    with (
        patch("apps.agents.graph.base.TenantSchema") as MockTS,
        patch("apps.agents.graph.base.get_registry") as mock_registry,
        patch("apps.agents.graph.base.sync_to_async") as mock_s2a,
        patch("apps.agents.graph.base._render_full_schema") as mock_full,
    ):
        MockTS.objects.filter.return_value.afirst = AsyncMock(return_value=mock_ts)
        mock_registry.return_value.get.return_value = MagicMock()
        mock_s2a.return_value = AsyncMock(return_value=mock_tables)
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
    from apps.projects.models import SchemaState

    mock_ts = MagicMock()
    mock_ts.state = SchemaState.ACTIVE

    mock_tables = [
        {
            "name": "cases",
            "description": "CommCare cases",
            "row_count": 100,
            "materialized_at": "2026-03-02T10:00:00",
        },
    ]

    small_column_text = (
        "**cases** — CommCare cases (100 rows)\nColumns:\n- case_id (text)\n- closed (boolean)\n"
    )

    with (
        patch("apps.agents.graph.base.TenantSchema") as MockTS,
        patch("apps.agents.graph.base.get_registry") as mock_registry,
        patch("apps.agents.graph.base.sync_to_async") as mock_s2a,
        patch("apps.agents.graph.base._render_full_schema") as mock_full,
        patch(
            "apps.agents.graph.base.load_tenant_context", new=AsyncMock(return_value=MagicMock())
        ),
        patch("apps.projects.models.TenantMetadata") as MockTM,
    ):
        MockTS.objects.filter.return_value.afirst = AsyncMock(return_value=mock_ts)
        mock_registry.return_value.get.return_value = MagicMock()
        # First sync_to_async call returns tables; subsequent calls (describe_table) return column dicts
        mock_s2a.side_effect = [
            AsyncMock(return_value=mock_tables),
            AsyncMock(return_value={"columns": [{"name": "case_id", "type": "text"}]}),
        ]
        mock_full.return_value = small_column_text
        MockTM.objects.filter.return_value.afirst = AsyncMock(return_value=None)

        result = await _fetch_schema_context(mock_tenant, mock_user)

    assert "case_id" in result or small_column_text in result
    assert "describe_table" not in result  # no fallback note in full tier


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_fetch_schema_context_no_get_schema_status_instruction(mock_tenant, mock_user):
    """The returned text must NOT instruct the agent to call get_schema_status."""
    from apps.projects.models import SchemaState

    mock_ts = MagicMock()
    mock_ts.state = SchemaState.ACTIVE

    with (
        patch("apps.agents.graph.base.TenantSchema") as MockTS,
        patch("apps.agents.graph.base.get_registry") as mock_registry,
        patch("apps.agents.graph.base.sync_to_async") as mock_s2a,
        patch("apps.agents.graph.base._render_full_schema") as mock_full,
    ):
        MockTS.objects.filter.return_value.afirst = AsyncMock(return_value=mock_ts)
        mock_registry.return_value.get.return_value = MagicMock()
        mock_s2a.return_value = AsyncMock(return_value=[])
        mock_full.return_value = ""

        result = await _fetch_schema_context(mock_tenant, mock_user)

    assert "call `get_schema_status`" not in result
    assert "start of every conversation" not in result


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_build_system_prompt_no_schema_status_call():
    """The assembled system prompt must not instruct the agent to call get_schema_status."""
    from apps.agents.graph.base import _build_system_prompt
    from apps.projects.models import SchemaState

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
        patch("apps.agents.graph.base.sync_to_async") as mock_s2a,
        patch("apps.agents.graph.base._render_full_schema") as mock_full,
    ):
        MockKR.return_value.retrieve = AsyncMock(return_value="")
        MockTS.objects.filter.return_value.afirst = AsyncMock(return_value=mock_ts)
        mock_reg.return_value.get.return_value = MagicMock()
        mock_s2a.return_value = AsyncMock(return_value=[])
        mock_full.return_value = ""

        prompt = await _build_system_prompt(mock_workspace, MagicMock())

    assert "call `get_schema_status`" not in prompt
    assert "start of every conversation" not in prompt
    assert "## Data Availability" in prompt
