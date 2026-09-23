"""Fault injection through the actual fixture, with all DB/DDL IO replaced."""

import asyncio
import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from . import test_artifact_view_provenance as subject

STAGES = ("pools", "view lookup", "view teardown", "schema 0", "schema 1", "connections")


@pytest.fixture
def fixture_io(settings, monkeypatch):
    settings.MANAGED_DATABASE_URL = "postgresql://synthetic:unused@127.0.0.1:1/unused"
    workspace = subject.Workspace(id=uuid4(), name="Cleanup-only workspace")
    user = subject.User(id=uuid4(), email="cleanup@example.com")
    tenants = [
        subject.Tenant(id=uuid4(), provider="commcare", external_id=f"cleanup-{number}")
        for number in range(2)
    ]
    schemas = [
        subject.TenantSchema(id=uuid4(), tenant=tenant, schema_name=f"cleanup_{number}")
        for number, tenant in enumerate(tenants)
    ]
    view = subject.WorkspaceViewSchema(id=uuid4(), workspace=workspace, schema_name="cleanup_view")
    dataset = subject.SemanticDataset(
        id=uuid4(), workspace=workspace, name="CleanupVisits", table_name="cleanup__raw_visits"
    )
    artifact = subject.Artifact(id=uuid4(), workspace=workspace, created_by=user)
    manager = Mock()
    manager.provision.side_effect = schemas
    manager.build_view_schema.return_value = view
    manager._view_prefix.return_value = "cleanup"
    monkeypatch.setattr(subject, "SchemaManager", Mock(return_value=manager))
    monkeypatch.setattr(subject.Workspace.objects, "acreate", AsyncMock(return_value=workspace))
    monkeypatch.setattr(subject.User.objects, "create_user", Mock(return_value=user))
    monkeypatch.setattr(subject.WorkspaceMembership.objects, "create", Mock())
    monkeypatch.setattr(subject.Tenant.objects, "create", Mock(side_effect=tenants))
    monkeypatch.setattr(subject.WorkspaceTenant.objects, "create", Mock())
    monkeypatch.setattr(subject, "grant_tenant_access", Mock())
    monkeypatch.setattr(subject, "_create_table", Mock())
    monkeypatch.setattr(subject, "build_and_promote_cube_schema", Mock(return_value=object()))
    monkeypatch.setattr(subject.SemanticDataset.objects, "aget", AsyncMock(return_value=dataset))
    monkeypatch.setattr(subject.Artifact.objects, "acreate", AsyncMock(return_value=artifact))
    monkeypatch.setattr(subject, "_login", Mock())

    events = []
    failures = {}
    lookup_result = [view]

    def perform(stage):
        events.append(stage)
        if stage in failures:
            raise failures[stage]

    async def pools():
        perform("pools")

    async def lookup():
        perform("view lookup")
        return lookup_result[0]

    def teardown_view(selected):
        assert selected is view
        perform("view teardown")

    manager.teardown_view_schema.side_effect = teardown_view

    def teardown_schema(selected):
        assert selected in schemas
        perform(f"schema {schemas.index(selected)}")

    manager.teardown.side_effect = teardown_schema
    monkeypatch.setattr(subject, "close_all_pools", AsyncMock(side_effect=pools))
    query = SimpleNamespace(afirst=AsyncMock(side_effect=lookup))
    view_filter = Mock(return_value=query)
    monkeypatch.setattr(subject.WorkspaceViewSchema.objects, "filter", view_filter)
    monkeypatch.setattr(
        subject.connections, "close_all", Mock(side_effect=lambda: perform("connections"))
    )
    return SimpleNamespace(
        manager=manager,
        schemas=schemas,
        view=view,
        workspace=workspace,
        events=events,
        failures=failures,
        lookup_result=lookup_result,
        view_filter=view_filter,
        generator=inspect.unwrap(subject.published_sources)(settings, monkeypatch),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_stages", [(), *((stage,) for stage in STAGES), STAGES])
async def test_each_owned_cleanup_is_attempted_and_errors_are_retained(fixture_io, failed_stages):
    setup = fixture_io
    await anext(setup.generator)
    originals = {stage: RuntimeError(f"Synthetic {stage} failure") for stage in failed_stages}
    setup.failures.update(originals)
    caught = None
    try:
        await setup.generator.aclose()
    except BaseException as error:  # Inspect cancellation/grouping without hiding any failure.
        caught = error

    assert setup.events == list(STAGES)
    setup.view_filter.assert_called_once_with(workspace=setup.workspace)
    setup.manager.teardown_view_schema.assert_called_once_with(setup.view)
    assert setup.manager.teardown.call_args_list[0].args == (setup.schemas[0],)
    assert setup.manager.teardown.call_args_list[1].args == (setup.schemas[1],)
    if failed_stages:
        assert isinstance(caught, ExceptionGroup)
        assert caught.exceptions == tuple(originals.values())
    else:
        assert caught is None


@pytest.mark.asyncio
async def test_partial_setup_failure_cleans_only_resources_already_owned(fixture_io):
    setup = fixture_io
    setup_error = RuntimeError("Synthetic second provision failure")
    setup.manager.provision.side_effect = [setup.schemas[0], setup_error]
    setup.lookup_result[0] = None
    with pytest.raises(RuntimeError, match="second provision") as failure:
        await anext(setup.generator)
    assert failure.value is setup_error
    assert setup.events == ["pools", "view lookup", "schema 0", "connections"]
    setup.manager.teardown.assert_called_once_with(setup.schemas[0])
    setup.manager.teardown_view_schema.assert_not_called()


@pytest.mark.asyncio
async def test_cleanup_error_does_not_hide_the_original_setup_failure(fixture_io):
    setup = fixture_io
    setup_error = RuntimeError("Synthetic second provision failure")
    pool_error = RuntimeError("Synthetic pool close failure")
    setup.manager.provision.side_effect = [setup.schemas[0], setup_error]
    setup.lookup_result[0] = None
    setup.failures["pools"] = pool_error
    with pytest.raises(ExceptionGroup) as failure:
        await anext(setup.generator)
    assert failure.value.exceptions == (pool_error,)
    assert failure.value.__context__ is setup_error
    assert setup.events == ["pools", "view lookup", "schema 0", "connections"]


@pytest.mark.asyncio
async def test_synchronous_view_lookup_failure_still_cleans_the_known_view(fixture_io):
    setup = fixture_io
    await anext(setup.generator)
    lookup_error = RuntimeError("Synthetic queryset construction failure")

    def failed_filter(**kwargs):
        assert kwargs == {"workspace": setup.workspace}
        setup.events.append("view lookup")
        raise lookup_error

    setup.view_filter.side_effect = failed_filter
    with pytest.raises(ExceptionGroup) as failure:
        await setup.generator.aclose()
    assert failure.value.exceptions == (lookup_error,)
    assert setup.events == list(STAGES)
    setup.manager.teardown_view_schema.assert_called_once_with(setup.view)


@pytest.mark.asyncio
async def test_pool_cancellation_is_preserved_after_remaining_cleanup(fixture_io):
    setup = fixture_io
    await anext(setup.generator)
    cancelled = asyncio.CancelledError("Synthetic close cancellation")
    setup.failures["pools"] = cancelled
    with pytest.raises(BaseExceptionGroup) as failure:
        await setup.generator.aclose()
    assert failure.value.exceptions == (cancelled,)
    assert setup.events == list(STAGES)
