"""The agent prompt's Source Descriptions block (#917)."""

import pytest

from apps.agents.graph.prompt_context import _source_descriptions_block
from apps.users.models import Tenant
from apps.workspaces.models import WorkspaceTenant


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_prompt_lists_each_described_source(workspace, tenant):
    other = await Tenant.objects.acreate(
        provider="ocs",
        external_id="bot",
        canonical_name="Helper Bot",
        description=" Line one\n\n line two ",
    )
    await WorkspaceTenant.objects.acreate(workspace=workspace, tenant=other)
    await Tenant.objects.acreate(
        provider="ocs", external_id="elsewhere", canonical_name="Not here", description="Hidden"
    )

    block = await _source_descriptions_block(workspace)

    assert "### Source Descriptions" in block
    assert "- Helper Bot (Open Chat Studio): Line one line two" in block
    assert "Not here" not in block
    assert tenant.canonical_name not in block


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_prompt_block_is_empty_without_descriptions(workspace):
    assert await _source_descriptions_block(workspace) == ""
