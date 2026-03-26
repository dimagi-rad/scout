"""Tests for lineage resolution and transformation-aware metadata (Milestone 6)."""

from unittest.mock import patch

import pytest

from apps.transformations.models import TransformationAsset, TransformationScope
from apps.transformations.services.lineage import get_lineage_chain, get_terminal_assets

# ---------------------------------------------------------------------------
# get_terminal_assets
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_no_assets_returns_empty(tenant):
    result = get_terminal_assets(tenant_ids=[tenant.id])
    assert result == []


@pytest.mark.django_db
def test_single_asset_no_replaces_is_terminal(tenant):
    asset = TransformationAsset.objects.create(
        name="stg_case_patient",
        scope=TransformationScope.SYSTEM,
        tenant=tenant,
        sql_content="SELECT * FROM raw_cases WHERE case_type = 'patient'",
    )
    result = get_terminal_assets(tenant_ids=[tenant.id])
    assert len(result) == 1
    assert result[0].id == asset.id


@pytest.mark.django_db
def test_replaced_asset_not_terminal(tenant):
    asset_a = TransformationAsset.objects.create(
        name="stg_case_patient",
        scope=TransformationScope.SYSTEM,
        tenant=tenant,
        sql_content="SELECT * FROM raw_cases WHERE case_type = 'patient'",
    )
    asset_b = TransformationAsset.objects.create(
        name="cases_clean",
        scope=TransformationScope.TENANT,
        tenant=tenant,
        sql_content="SELECT * FROM {{ ref('stg_case_patient') }}",
        replaces=asset_a,
    )
    result = get_terminal_assets(tenant_ids=[tenant.id])
    assert len(result) == 1
    assert result[0].id == asset_b.id


@pytest.mark.django_db
def test_three_deep_chain_terminal(tenant):
    """Chain: A → B → C (C replaces B, B replaces A) → terminal is C."""
    asset_a = TransformationAsset.objects.create(
        name="stg_case_patient",
        scope=TransformationScope.SYSTEM,
        tenant=tenant,
        sql_content="SELECT * FROM raw_cases",
    )
    asset_b = TransformationAsset.objects.create(
        name="cases_clean",
        scope=TransformationScope.TENANT,
        tenant=tenant,
        sql_content="SELECT * FROM {{ ref('stg_case_patient') }}",
        replaces=asset_a,
    )
    asset_c = TransformationAsset.objects.create(
        name="cases_final",
        scope=TransformationScope.TENANT,
        tenant=tenant,
        sql_content="SELECT * FROM {{ ref('cases_clean') }}",
        replaces=asset_b,
    )
    result = get_terminal_assets(tenant_ids=[tenant.id])
    assert len(result) == 1
    assert result[0].id == asset_c.id


@pytest.mark.django_db
def test_multiple_independent_chains(tenant):
    """Two independent chains → returns both terminal nodes."""
    case_a = TransformationAsset.objects.create(
        name="stg_case_patient",
        scope=TransformationScope.SYSTEM,
        tenant=tenant,
        sql_content="SELECT * FROM raw_cases WHERE case_type = 'patient'",
    )
    case_b = TransformationAsset.objects.create(
        name="cases_clean",
        scope=TransformationScope.TENANT,
        tenant=tenant,
        sql_content="SELECT * FROM {{ ref('stg_case_patient') }}",
        replaces=case_a,
    )
    form_standalone = TransformationAsset.objects.create(
        name="stg_form_registration",
        scope=TransformationScope.SYSTEM,
        tenant=tenant,
        sql_content="SELECT * FROM raw_forms WHERE xmlns = 'reg'",
    )
    result = get_terminal_assets(tenant_ids=[tenant.id])
    terminal_ids = {a.id for a in result}
    assert terminal_ids == {case_b.id, form_standalone.id}


@pytest.mark.django_db
def test_assets_filtered_by_tenant_ids(tenant):
    """Assets from different tenants are filtered correctly."""
    from apps.users.models import Tenant

    other_tenant = Tenant.objects.create(
        provider="commcare", external_id="other-domain", canonical_name="Other"
    )
    TransformationAsset.objects.create(
        name="stg_case_patient",
        scope=TransformationScope.SYSTEM,
        tenant=tenant,
        sql_content="SELECT * FROM raw_cases",
    )
    TransformationAsset.objects.create(
        name="stg_case_other",
        scope=TransformationScope.SYSTEM,
        tenant=other_tenant,
        sql_content="SELECT * FROM raw_cases",
    )
    result = get_terminal_assets(tenant_ids=[tenant.id])
    assert len(result) == 1
    assert result[0].name == "stg_case_patient"


@pytest.mark.django_db
def test_workspace_assets_included(workspace):
    ws_asset = TransformationAsset.objects.create(
        name="my_analysis",
        scope=TransformationScope.WORKSPACE,
        workspace=workspace,
        sql_content="SELECT * FROM stg_cases",
    )
    result = get_terminal_assets(tenant_ids=[], workspace_id=workspace.id)
    assert len(result) == 1
    assert result[0].id == ws_asset.id


# ---------------------------------------------------------------------------
# get_lineage_chain
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_lineage_no_replaces(tenant):
    TransformationAsset.objects.create(
        name="stg_case_patient",
        scope=TransformationScope.SYSTEM,
        tenant=tenant,
        sql_content="SELECT * FROM raw_cases",
        description="Staging model for patient cases",
    )
    chain = get_lineage_chain("stg_case_patient", tenant_ids=[tenant.id])
    assert len(chain) == 1
    assert chain[0]["name"] == "stg_case_patient"
    assert chain[0]["scope"] == "system"
    assert chain[0]["description"] == "Staging model for patient cases"


@pytest.mark.django_db
def test_lineage_with_replaces(tenant):
    asset_a = TransformationAsset.objects.create(
        name="stg_case_patient",
        scope=TransformationScope.SYSTEM,
        tenant=tenant,
        sql_content="SELECT * FROM raw_cases",
        description="Raw staging",
    )
    TransformationAsset.objects.create(
        name="cases_clean",
        scope=TransformationScope.TENANT,
        tenant=tenant,
        sql_content="SELECT * FROM {{ ref('stg_case_patient') }}",
        description="Cleaned cases",
        replaces=asset_a,
    )
    chain = get_lineage_chain("cases_clean", tenant_ids=[tenant.id])
    assert len(chain) == 2
    assert chain[0]["name"] == "cases_clean"
    assert chain[1]["name"] == "stg_case_patient"


@pytest.mark.django_db
def test_lineage_three_deep(tenant):
    asset_a = TransformationAsset.objects.create(
        name="stg_case_patient",
        scope=TransformationScope.SYSTEM,
        tenant=tenant,
        sql_content="SELECT * FROM raw_cases",
    )
    asset_b = TransformationAsset.objects.create(
        name="cases_clean",
        scope=TransformationScope.TENANT,
        tenant=tenant,
        sql_content="SELECT * FROM {{ ref('stg_case_patient') }}",
        replaces=asset_a,
    )
    TransformationAsset.objects.create(
        name="cases_final",
        scope=TransformationScope.TENANT,
        tenant=tenant,
        sql_content="SELECT * FROM {{ ref('cases_clean') }}",
        replaces=asset_b,
    )
    chain = get_lineage_chain("cases_final", tenant_ids=[tenant.id])
    assert len(chain) == 3
    assert [c["name"] for c in chain] == ["cases_final", "cases_clean", "stg_case_patient"]


@pytest.mark.django_db
def test_lineage_not_found(tenant):
    chain = get_lineage_chain("nonexistent", tenant_ids=[tenant.id])
    assert chain == []


@pytest.mark.django_db
def test_lineage_cycle_guard(tenant):
    """If somehow A replaces B replaces A, doesn't infinite loop."""
    asset_a = TransformationAsset.objects.create(
        name="stg_alpha",
        scope=TransformationScope.SYSTEM,
        tenant=tenant,
        sql_content="SELECT 1",
    )
    asset_b = TransformationAsset.objects.create(
        name="stg_beta",
        scope=TransformationScope.SYSTEM,
        tenant=tenant,
        sql_content="SELECT 1",
        replaces=asset_a,
    )
    # Force a cycle via raw update to bypass any validation
    TransformationAsset.objects.filter(id=asset_a.id).update(replaces=asset_b)

    chain = get_lineage_chain("stg_beta", tenant_ids=[tenant.id])
    # Should terminate without infinite loop; exact length depends on traversal
    assert len(chain) <= 3
    assert chain[0]["name"] == "stg_beta"


# ---------------------------------------------------------------------------
# transformation_aware_list_tables
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_transformation_aware_no_assets_fallback(tenant):
    """No transformation assets → falls back to pipeline_list_tables."""
    from mcp_server.services.metadata import transformation_aware_list_tables

    mock_tables = [{"name": "raw_cases", "type": "table", "description": "", "row_count": 10}]

    with patch(
        "mcp_server.services.metadata.pipeline_list_tables", return_value=mock_tables
    ) as mock_plt:
        result = transformation_aware_list_tables(
            tenant_schema=None,  # not used when mocked
            pipeline_config=None,
            tenant_ids=[tenant.id],
        )
    mock_plt.assert_called_once()
    assert result == mock_tables


@pytest.mark.django_db
def test_transformation_aware_terminal_replaces_raw(tenant):
    """Terminal asset replacing a raw table: raw table excluded, terminal included."""
    from mcp_server.services.metadata import transformation_aware_list_tables

    raw_asset = TransformationAsset.objects.create(
        name="stg_case_patient",
        scope=TransformationScope.SYSTEM,
        tenant=tenant,
        sql_content="SELECT * FROM raw_cases",
        description="Staging cases",
    )
    TransformationAsset.objects.create(
        name="cases_clean",
        scope=TransformationScope.TENANT,
        tenant=tenant,
        sql_content="SELECT * FROM {{ ref('stg_case_patient') }}",
        description="Cleaned cases",
        replaces=raw_asset,
    )

    mock_raw_tables = [
        {"name": "raw_cases", "type": "table", "description": "Cases", "row_count": 100},
        {"name": "raw_forms", "type": "table", "description": "Forms", "row_count": 50},
    ]

    with patch("mcp_server.services.metadata.pipeline_list_tables", return_value=mock_raw_tables):
        result = transformation_aware_list_tables(
            tenant_schema=None,
            pipeline_config=None,
            tenant_ids=[tenant.id],
        )

    names = {t["name"] for t in result}
    # stg_case_patient is replaced so should not appear
    assert "stg_case_patient" not in names
    # cases_clean is terminal and should appear
    assert "cases_clean" in names
    # raw tables that aren't replaced should still be there
    assert "raw_cases" in names
    assert "raw_forms" in names


@pytest.mark.django_db
def test_transformation_aware_mixed(tenant):
    """Mix: some raw tables have no replacing asset → they appear alongside terminals."""
    from mcp_server.services.metadata import transformation_aware_list_tables

    TransformationAsset.objects.create(
        name="stg_form_reg",
        scope=TransformationScope.SYSTEM,
        tenant=tenant,
        sql_content="SELECT * FROM raw_forms",
        description="Registration form staging",
    )

    mock_raw_tables = [
        {"name": "raw_cases", "type": "table", "description": "Cases", "row_count": 100},
        {"name": "raw_forms", "type": "table", "description": "Forms", "row_count": 50},
    ]

    with patch("mcp_server.services.metadata.pipeline_list_tables", return_value=mock_raw_tables):
        result = transformation_aware_list_tables(
            tenant_schema=None,
            pipeline_config=None,
            tenant_ids=[tenant.id],
        )

    names = {t["name"] for t in result}
    # standalone terminal asset appears alongside raw tables
    assert "stg_form_reg" in names
    assert "raw_cases" in names
    assert "raw_forms" in names
