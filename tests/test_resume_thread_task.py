from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model

from apps.chat.models import Thread, ThreadJob
from apps.users.models import Tenant
from apps.workspaces.models import (
    MaterializationRun,
    TenantSchema,
    Workspace,
    WorkspaceTenant,
)

User = get_user_model()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_resume_appends_system_message_and_invokes_agent():
    user = await sync_to_async(User.objects.create_user)(email="a@b.c", password="x")
    ws = await sync_to_async(Workspace.objects.create)(name="W", created_by=user)
    tenant = await sync_to_async(Tenant.objects.create)(
        external_id="t1", provider="commcare", canonical_name="Test Tenant"
    )
    await sync_to_async(WorkspaceTenant.objects.create)(workspace=ws, tenant=tenant)
    schema = await sync_to_async(TenantSchema.objects.create)(tenant=tenant, schema_name="s_t1")
    thread = await sync_to_async(Thread.objects.create)(workspace=ws, user=user)
    tj = await sync_to_async(ThreadJob.objects.create)(
        thread=thread, job_type="materialization",
        procrastinate_job_id=3003, tool_call_id="tc3",
        state=ThreadJob.State.RUNNING,
    )
    await sync_to_async(MaterializationRun.objects.create)(
        tenant_schema=schema, pipeline="commcare_sync",
        state=MaterializationRun.RunState.COMPLETED,
        procrastinate_job_id=3003,
        result={"rows": 50000},
    )

    from apps.workspaces.tasks import resume_thread_after_materialization

    mock_agent = MagicMock()
    mock_agent.ainvoke = AsyncMock(return_value={"messages": []})
    with patch(
        "apps.workspaces.tasks._build_agent_for_resume",
        AsyncMock(return_value=(mock_agent, {})),
    ):
        result = await resume_thread_after_materialization(
            None, thread_job_id=str(tj.id),
        )

    assert result["status"] == "resumed"
    await sync_to_async(tj.refresh_from_db)()
    assert tj.state == ThreadJob.State.COMPLETED
    # Inspect the input_state passed to ainvoke.
    call_args = mock_agent.ainvoke.await_args
    input_state = call_args.args[0]
    messages = input_state["messages"]
    assert len(messages) == 1
    assert messages[0].content.startswith("[__system_resume__]")
    assert "completed" in messages[0].content
    # Confirm oauth_tokens is forwarded into the runtime config.
    config = call_args.args[1]
    assert "oauth_tokens" in config
