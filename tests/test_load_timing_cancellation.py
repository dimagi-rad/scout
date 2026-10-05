import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from apps.workspaces.services.materialize import materialize_workspace


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_timing_cancellation_cannot_skip_chat_resume(workspace):
    context = SimpleNamespace(job=SimpleNamespace(id=100))
    with (
        patch(
            "apps.workspaces.services.materialize.materialize_workspace_core",
            AsyncMock(return_value={}),
        ),
        patch(
            "apps.workspaces.services.materialize.afinish_load_timing",
            AsyncMock(side_effect=asyncio.CancelledError),
        ),
        patch("apps.workspaces.services.materialize._defer_resume_for_job", AsyncMock()) as resume,
        patch("apps.workspaces.services.materialize._defer_pending_flush", AsyncMock()),
    ):
        with pytest.raises(asyncio.CancelledError):
            await materialize_workspace(context, str(workspace.id))
    resume.assert_awaited_once()
