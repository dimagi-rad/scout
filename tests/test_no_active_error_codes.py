"""One error code per axis for "nothing active to query" (#251 item 6).

Five raise sites described a missing serving schema or semantic model in five
wordings, and the semantic query flattened three of them into VALIDATION_ERROR,
so nothing downstream could tell "load the data" from "fix the query".
"""

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from apps.agents.graph.base import _should_escalate
from apps.common.error_codes import ErrorCode
from apps.common.errors import DataNotLoaded, ExpectedStateError
from apps.semantic.services import query as query_service
from apps.semantic.services.catalog import no_active_semantic_model
from apps.semantic.services.cube_schema import get_active_cube_schema
from apps.users.models import Tenant
from apps.workspaces.models import WorkspaceTenant
from apps.workspaces.services.schema_manager import NoActiveTenantSchema
from mcp_server import server
from mcp_server.context import load_workspace_context

DATA_NOT_LOADED = ErrorCode.DATA_NOT_LOADED
SEMANTIC_MODEL_UNAVAILABLE = ErrorCode.SEMANTIC_MODEL_UNAVAILABLE
QUERY = {"measures": ["visits.count"], "limit": 10}


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("sources", [1, 2], ids=["single-source", "multi-source"])
async def test_a_workspace_without_a_serving_schema_raises_data_not_loaded(workspace, sources):
    if sources == 2:
        other = await Tenant.objects.acreate(provider="commcare", external_id="other-domain")
        await WorkspaceTenant.objects.acreate(workspace=workspace, tenant=other)

    with pytest.raises(DataNotLoaded) as caught:
        await load_workspace_context(str(workspace.id))

    assert caught.value.code == DATA_NOT_LOADED
    assert isinstance(caught.value, ExpectedStateError)
    assert isinstance(caught.value, ValueError)


def test_a_view_build_with_no_serving_source_is_data_not_loaded():
    assert NoActiveTenantSchema("ws-id").code == DATA_NOT_LOADED


def test_missing_semantic_layer_shares_one_code():
    assert no_active_semantic_model().code == SEMANTIC_MODEL_UNAVAILABLE


@pytest.mark.django_db
def test_a_missing_cube_schema_is_semantic_model_unavailable(workspace):
    with pytest.raises(Exception) as caught:
        get_active_cube_schema(workspace, model=None)
    assert caught.value.code == SEMANTIC_MODEL_UNAVAILABLE


async def _query_error(monkeypatch, workspace, **patches):
    for name, value in patches.items():
        monkeypatch.setattr(query_service, name, value)
    result = await query_service.run_semantic_query(workspace, QUERY)
    assert result["success"] is False
    assert result["error"]["category"] == "data_unavailable"
    return result["error"]["code"]


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_semantic_query_reports_a_missing_model_by_its_own_code(monkeypatch, workspace):
    def missing_model(_workspace):
        raise no_active_semantic_model()

    code = await _query_error(monkeypatch, workspace, get_active_semantic_model=missing_model)
    assert code == SEMANTIC_MODEL_UNAVAILABLE


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_semantic_query_reports_unloaded_data_by_its_own_code(monkeypatch, workspace):
    def compiled(_workspace, _spec):
        return {"model": None, "cube_schema": None}

    async def not_loaded(_workspace_id):
        raise DataNotLoaded("No active data schema for workspace 'x'.")

    code = await _query_error(
        monkeypatch,
        workspace,
        _compile_semantic_query_for_async=compiled,
        load_workspace_context=not_loaded,
    )
    assert code == DATA_NOT_LOADED


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_describe_dataset_reports_a_missing_model_by_its_own_code(workspace, user):
    result = await server.describe_dataset(
        "visits", workspace_id=str(workspace.id), user_id=str(user.id)
    )
    assert result["success"] is False
    assert result["error"]["code"] == SEMANTIC_MODEL_UNAVAILABLE


@pytest.mark.parametrize("code", [DATA_NOT_LOADED, SEMANTIC_MODEL_UNAVAILABLE])
def test_repeated_not_loaded_errors_still_escalate(code):
    """They were VALIDATION_ERROR, which escalates; the new codes must too."""
    error = f'{{"success": false, "error": {{"code": "{code}", "message": "m"}}}}'
    messages = [HumanMessage(content="q")]
    for i in range(3):
        messages.append(
            AIMessage(content="", tool_calls=[{"name": "t", "args": {}, "id": f"c{i}"}])
        )
        messages.append(ToolMessage(content=error, tool_call_id=f"c{i}"))
    assert _should_escalate(messages)
