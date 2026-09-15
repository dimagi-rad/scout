"""Distinct source case types must never overwrite the same staging model."""

import re

import pytest
from django.db import connection

from apps.common.identifiers import fit_identifier
from apps.transformations.services.commcare_staging import (
    generate_system_assets,
    upsert_system_assets,
)
from apps.users.models import Tenant
from apps.workspaces.models import TenantMetadata


def metadata(*names):
    return {"case_types": [{"name": name} for name in names]}


def generate(*names):
    tenant = Tenant(provider="commcare", external_id="synthetic-case-collisions")
    return generate_system_assets(tenant, metadata(*names))


@pytest.mark.parametrize(
    "names",
    [
        ("Household", "household"),
        ("child-care", "child_care"),
        ("child care", "child-care", "child_care"),
        ("child" + "x" * 90 + "-care", "child" + "x" * 90 + "_care"),
    ],
)
def test_colliding_case_types_get_distinct_bounded_names(names):
    assets = generate(*names)
    assert len({asset.name for asset in assets}) == len(names)
    assert all(len(asset.name.encode()) <= 63 for asset in assets)
    assert all(re.fullmatch(r"[a-z][a-z0-9_]*", asset.name) for asset in assets)
    for name, asset in zip(names, assets, strict=True):
        assert f"WHERE case_type = '{name}'" in asset.sql_content


def test_collision_assignment_is_order_independent_and_preserves_unrelated_names():
    names = ("Household", "household", "patient")
    first = {asset.sql_content: asset.name for asset in generate(*names)}
    reverse = {asset.sql_content: asset.name for asset in generate(*reversed(names))}
    assert first == reverse
    assert len(set(first.values())) == len(names)
    assert (
        next(asset.name for asset in generate(*names) if "'patient'" in asset.sql_content)
        == "stg_case_patient"
    )


def test_hashed_collision_name_does_not_steal_an_existing_literal_name():
    candidate = fit_identifier(
        "stg_case_child_care", unique_key="case:child-care", always_hash=True
    )
    natural_name = candidate.removeprefix("stg_case_")
    assets = generate("child-care", "child_care", natural_name)
    assert len({asset.name for asset in assets}) == 3
    assert assets[-1].name == candidate


def test_repeated_identical_case_type_generates_one_model():
    assert len(generate("patient", "patient")) == 1


@pytest.mark.django_db
def test_case_collision_models_preserve_distinct_source_rows():
    assets = generate("Household", "household")
    with connection.cursor() as cursor:
        cursor.execute(
            "CREATE TEMP TABLE raw_cases (case_id text, case_type text, case_name text, "
            "owner_id text, date_opened text, last_modified text, closed boolean, properties jsonb) "
            "ON COMMIT DROP"
        )
        cursor.execute(
            "INSERT INTO raw_cases VALUES "
            "('upper', 'Household', 'Synthetic', 'owner', '2026-09-01', '2026-09-01', false, '{}'), "
            "('lower', 'household', 'Synthetic', 'owner', '2026-09-01', '2026-09-01', false, '{}')"
        )
        results = {}
        for asset in assets:
            cursor.execute(asset.sql_content)
            results[asset.name] = [row[0] for row in cursor.fetchall()]
    assert len(results) == 2
    assert sorted(results.values()) == [["lower"], ["upper"]]


@pytest.mark.django_db
def test_upsert_keeps_both_case_types_and_does_not_overwrite_one():
    tenant = Tenant.objects.create(provider="commcare", external_id="synthetic-case-collisions")
    tenant_metadata = TenantMetadata.objects.create(
        tenant=tenant, metadata=metadata("Household", "household")
    )
    first = upsert_system_assets(tenant, tenant_metadata)
    assert first["created"] == first["total"] == 2
    second = upsert_system_assets(tenant, tenant_metadata)
    assert second["created"] == 0
    assert second["updated"] == second["total"] == 2
