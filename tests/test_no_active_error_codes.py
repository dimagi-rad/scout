"""One error code per axis for "nothing active to query" (#251 item 6).

Five raise sites described a missing serving schema or semantic model in five
wordings, and the semantic query flattened three of them into VALIDATION_ERROR,
so nothing downstream could tell "load the data" from "fix the query".
"""

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from apps.agents.graph.run_policy import (
    ESCALATION_MESSAGE,
    READ_ONLY_ESCALATION_MESSAGE,
    READ_ONLY_SEMANTIC_ESCALATION_MESSAGE,
    SEMANTIC_ESCALATION_MESSAGE,
    _schema_escalation_message,
    _should_escalate,
)
from apps.common.error_codes import ErrorCode
from apps.common.errors import DataNotLoaded, ExpectedStateError
from apps.semantic.models import SemanticDataset, SemanticModel
from apps.semantic.services import catalog as catalog_service
from apps.semantic.services import query as query_service
from apps.semantic.services.catalog import (
    SemanticCatalogUnavailable,
    no_active_semantic_model,
    serialize_dataset,
)
from apps.semantic.services.cube_schema import NoActiveCubeSchema, get_active_cube_schema
from apps.users.models import Tenant
from apps.workspaces.models import SchemaState, TenantSchema, WorkspaceTenant
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
    model = SemanticModel.objects.create(workspace=workspace, name="No cube")
    with pytest.raises(NoActiveCubeSchema) as caught:
        get_active_cube_schema(workspace, model=model)
    assert caught.value.code == SEMANTIC_MODEL_UNAVAILABLE
    assert isinstance(caught.value, ExpectedStateError)


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
    def compiled(_workspace, _spec, _max_limit):
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


def _repeated(code):
    error = f'{{"success": false, "error": {{"code": "{code}", "message": "m"}}}}'
    messages = [HumanMessage(content="q")]
    for i in range(3):
        messages.append(
            AIMessage(content="", tool_calls=[{"name": "t", "args": {}, "id": f"c{i}"}])
        )
        messages.append(ToolMessage(content=error, tool_call_id=f"c{i}"))
    return messages


@pytest.mark.parametrize("code", [DATA_NOT_LOADED, SEMANTIC_MODEL_UNAVAILABLE])
def test_codes_that_used_to_be_validation_errors_still_escalate(code):
    assert _should_escalate(_repeated(code))


@pytest.mark.parametrize("code", [ErrorCode.SCHEMA_BUILD_FAILED, ErrorCode.PIPELINE_UNRESOLVED])
def test_failures_a_reload_cannot_fix_do_not_escalate_to_a_reload(code):
    """get_schema_status and the SQL tools already sent these, and never escalated."""
    assert not _should_escalate(_repeated(code))


@pytest.mark.parametrize(
    ("code", "write_capable", "expected"),
    [
        (SEMANTIC_MODEL_UNAVAILABLE, True, SEMANTIC_ESCALATION_MESSAGE),
        (SEMANTIC_MODEL_UNAVAILABLE, False, READ_ONLY_SEMANTIC_ESCALATION_MESSAGE),
        (DATA_NOT_LOADED, True, ESCALATION_MESSAGE),
        (DATA_NOT_LOADED, False, READ_ONLY_ESCALATION_MESSAGE),
    ],
)
def test_a_missing_data_model_escalates_to_a_rebuild_not_a_reload(code, write_capable, expected):
    message = _schema_escalation_message(
        _repeated(code), write_capable=write_capable, interactive=True
    )
    assert message == expected


def test_an_earlier_successful_round_does_not_hide_a_missing_model():
    messages = [
        HumanMessage(content="q"),
        AIMessage(content="", tool_calls=[{"name": "list_datasets", "args": {}, "id": "ok"}]),
        ToolMessage(content='{"success": true, "data": {}}', tool_call_id="ok"),
        *_repeated(SEMANTIC_MODEL_UNAVAILABLE)[1:],
    ]
    message = _schema_escalation_message(messages, write_capable=True, interactive=True)
    assert message == SEMANTIC_ESCALATION_MESSAGE


def test_the_message_reads_the_same_streak_that_escalated():
    """A success older than the matched streak, even in the same round, does not count."""
    error = '{"success": false, "error": {"code": "SEMANTIC_MODEL_UNAVAILABLE", "message": "m"}}'
    calls = [{"name": "t", "args": {}, "id": f"c{i}"} for i in range(4)]
    messages = [
        HumanMessage(content="q"),
        AIMessage(content="", tool_calls=calls),
        ToolMessage(content='{"success": true, "data": {}}', tool_call_id="c0"),
        *(ToolMessage(content=error, tool_call_id=f"c{i}") for i in range(1, 4)),
    ]
    assert _should_escalate(messages)
    message = _schema_escalation_message(messages, write_capable=True, interactive=True)
    assert message == SEMANTIC_ESCALATION_MESSAGE


def test_a_mixed_round_is_not_blamed_on_the_data_model():
    messages = _repeated(SEMANTIC_MODEL_UNAVAILABLE)
    messages.insert(
        -1,
        ToolMessage(
            content='{"success": false, "error": {"code": "DATA_NOT_LOADED", "message": "m"}}',
            tool_call_id="parallel",
        ),
    )
    message = _schema_escalation_message(messages, write_capable=True, interactive=True)
    assert message == ESCALATION_MESSAGE


@pytest.mark.django_db
def test_a_hidden_dataset_is_not_reported_as_a_missing_model(workspace):
    model = SemanticModel.objects.create(workspace=workspace, name="Model")
    hidden = SemanticDataset.objects.create(
        semantic_model=model,
        workspace=workspace,
        name="hidden",
        label="Hidden",
        table_name="raw_hidden",
        schema_name="s",
        is_visible=False,
    )
    with pytest.raises(SemanticCatalogUnavailable) as caught:
        serialize_dataset(hidden)
    assert caught.value.code == ErrorCode.VALIDATION_ERROR


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("serving", "code"),
    [(False, DATA_NOT_LOADED), (True, ErrorCode.INTERNAL_ERROR)],
    ids=["not-loaded", "serving-but-unreadable"],
)
def test_an_unreadable_catalog_claims_not_loaded_only_when_nothing_serves(
    monkeypatch, workspace, tenant, serving, code
):
    if serving:
        TenantSchema.objects.create(tenant=tenant, schema_name="s", state=SchemaState.ACTIVE)

    async def broken(_workspace):
        raise RuntimeError("transient read failure")

    monkeypatch.setattr(catalog_service, "_load_physical_tables_async", broken)
    with pytest.raises(SemanticCatalogUnavailable) as caught:
        catalog_service.load_physical_tables(workspace)
    assert caught.value.code == code


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_sql_tools_report_unloaded_data_by_its_own_code(workspace, user):
    result = await server.list_tables(workspace_id=str(workspace.id), user_id=str(user.id))
    assert result["success"] is False
    assert result["error"]["code"] == DATA_NOT_LOADED


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_list_datasets_names_the_code_of_a_workspace_without_a_model(workspace, user):
    result = await server.list_datasets(workspace_id=str(workspace.id), user_id=str(user.id))
    errors = result["data"]["workspace_errors"]
    assert [e["code"] for e in errors] == [SEMANTIC_MODEL_UNAVAILABLE]
