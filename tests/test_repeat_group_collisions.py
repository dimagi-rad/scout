"""Distinct repeat groups of one parent must never overwrite the same staging model."""

import re

import pytest

from apps.common.identifiers import fit_identifier
from apps.transformations.models import TransformationAsset, TransformationScope
from apps.transformations.services.commcare_staging import (
    generate_system_assets,
    upsert_system_assets,
)
from apps.transformations.services.connect_staging import (
    generate_connect_assets,
    upsert_connect_assets,
)
from apps.users.models import Tenant
from apps.workspaces.models import TenantMetadata

VALID_NAME = r"[a-z][a-z0-9_]*"


def form_metadata(*group_paths, form_name="Reg"):
    questions = [{"value": f"{path}/q", "repeat": path, "type": "Text"} for path in group_paths]
    return {"form_definitions": {"urn:synthetic:form": {"name": form_name, "questions": questions}}}


def connect_form_definitions(*group_paths):
    questions = [{"value": f"{path}/q", "repeat": True, "type": "Text"} for path in group_paths]
    return {"visit": {"name": "Visit", "questions": questions}}


def generate(*group_paths, form_name="Reg"):
    tenant = Tenant(provider="commcare", external_id="synthetic-repeat-collisions")
    return generate_system_assets(tenant, form_metadata(*group_paths, form_name=form_name))


def generate_connect(*group_paths):
    tenant = Tenant(provider="commcare_connect", external_id="synthetic-repeat-collisions")
    return generate_connect_assets(connect_form_definitions(*group_paths), tenant)


def json_path(group_path):
    return "ARRAY[" + ",".join(f"'{p}'" for p in group_path.split("/") if p) + "]::text[]"


@pytest.mark.parametrize(
    "group_paths",
    [
        ("/data/child/vaccines", "/data/mother/vaccines"),
        ("/data/A", "/data/a"),
        ("/data/x/a", "/data/y/a", "/data/A"),
        ("/data/child/vaccine-log", "/data/child/vaccine_log"),
        ("/data/x/" + "v" * 90, "/data/y/" + "v" * 90),
    ],
)
def test_colliding_repeat_groups_get_distinct_bounded_names(group_paths):
    parent, *repeats = generate(*group_paths)
    assert parent.name == "stg_form_reg"
    assert len({asset.name for asset in repeats}) == len(group_paths)
    assert all(len(asset.name.encode()) <= 63 for asset in repeats)
    assert all(re.fullmatch(VALID_NAME, asset.name) for asset in repeats)
    for group_path, asset in zip(group_paths, repeats, strict=True):
        assert json_path(group_path) in asset.sql_content
        assert "{{ ref('stg_form_reg') }}" in asset.sql_content


def test_collision_assignment_is_order_independent_and_preserves_unrelated_names():
    paths = ("/data/child/vaccines", "/data/mother/vaccines", "/data/visits")
    first = {asset.sql_content: asset.name for asset in generate(*paths)}
    reverse = {asset.sql_content: asset.name for asset in generate(*reversed(paths))}
    assert first == reverse
    assert len(set(first.values())) == len(paths) + 1
    visits = next(
        asset for asset in generate(*paths) if json_path("/data/visits") in asset.sql_content
    )
    assert visits.name == "stg_form_reg__repeat_visits"


def test_non_colliding_repeat_group_names_are_unchanged():
    names = [asset.name for asset in generate("/data/children", "/data/visits")]
    assert names == ["stg_form_reg", "stg_form_reg__repeat_children", "stg_form_reg__repeat_visits"]


def test_hashed_collision_name_does_not_steal_an_existing_literal_name():
    candidate = fit_identifier(
        "stg_form_reg__repeat_a", unique_key="repeat:stg_form_reg\0/data/x/a", always_hash=True
    )
    natural_leaf = candidate.removeprefix("stg_form_reg__repeat_")
    _, *repeats = generate("/data/x/a", "/data/y/a", f"/data/{natural_leaf}")
    assert len({asset.name for asset in repeats}) == 3
    assert repeats[-1].name == candidate


def test_digest_named_repeat_groups_ref_a_digest_fitted_parent():
    form_name = "household registration follow up visit " + "x" * 60
    parent, *repeats = generate(
        "/data/child/vaccines", "/data/mother/vaccines", form_name=form_name
    )
    assert len(parent.name.encode()) <= 63
    assert len({asset.name for asset in repeats}) == 2
    assert all(len(asset.name.encode()) <= 63 for asset in repeats)
    assert all(f"{{{{ ref('{parent.name}') }}}}" in asset.sql_content for asset in repeats)


def test_connect_colliding_repeat_groups_get_distinct_names():
    paths = ("/data/child/vaccines", "/data/mother/vaccines", "/data/visits")
    visits, *repeats = generate_connect(*paths)
    assert visits.name == "stg_visits"
    assert len({asset.name for asset in repeats}) == len(paths)
    assert all(len(asset.name.encode()) <= 63 for asset in repeats)
    assert all(re.fullmatch(VALID_NAME, asset.name) for asset in repeats)
    assert all("{{ ref('stg_visits') }}" in asset.sql_content for asset in repeats)
    assert repeats[-1].name == "stg_visits__repeat_visits"
    forward = {asset.sql_content: asset.name for asset in repeats}
    reverse = {asset.sql_content: asset.name for asset in generate_connect(*reversed(paths))[1:]}
    assert forward == reverse


@pytest.mark.django_db
def test_upsert_keeps_both_repeat_groups_and_does_not_overwrite_one():
    tenant = Tenant.objects.create(provider="commcare", external_id="synthetic-repeat-collisions")
    tenant_metadata = TenantMetadata.objects.create(
        tenant=tenant, metadata=form_metadata("/data/child/vaccines", "/data/mother/vaccines")
    )
    first = upsert_system_assets(tenant, tenant_metadata)
    assert first["created"] == first["total"] == 3
    second = upsert_system_assets(tenant, tenant_metadata)
    assert second["created"] == 0
    assert second["updated"] == second["total"] == 3
    persisted = TransformationAsset.objects.filter(tenant=tenant, scope=TransformationScope.SYSTEM)
    assert persisted.count() == 3
    sql = {asset.sql_content for asset in persisted}
    assert any(json_path("/data/child/vaccines") in s for s in sql)
    assert any(json_path("/data/mother/vaccines") in s for s in sql)


@pytest.mark.django_db
def test_connect_upsert_keeps_both_repeat_groups():
    tenant = Tenant.objects.create(
        provider="commcare_connect", external_id="synthetic-repeat-collisions"
    )
    tenant_metadata = TenantMetadata.objects.create(
        tenant=tenant,
        metadata={
            "form_definitions": connect_form_definitions(
                "/data/child/vaccines", "/data/mother/vaccines"
            )
        },
    )
    assert upsert_connect_assets(tenant, tenant_metadata)["created"] == 3
    second = upsert_connect_assets(tenant, tenant_metadata)
    assert second["created"] == 0
    assert second["updated"] == second["total"] == 3
