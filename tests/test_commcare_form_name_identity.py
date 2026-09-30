"""Duplicate CommCare form names are keyed by xmlns, never by metadata order (#470)."""

import re

import pytest

from apps.common.identifiers import PG_MAX_IDENTIFIER_BYTES, dbt_model_name, view_name
from apps.transformations.models import TransformationAsset, TransformationScope
from apps.transformations.services.commcare_staging import (
    generate_system_assets,
    upsert_system_assets,
)
from apps.users.models import Tenant
from apps.workspaces.models import TenantMetadata
from apps.workspaces.services.schema_manager import SchemaManager

_REF = re.compile(r"ref\('([a-z0-9_]+)'\)")
_ALIAS = re.compile(r' AS "([^"]+)"')
LONG = "Household Registration And Follow Up Visit For Community Health Workers " * 3


@pytest.fixture
def tenant():
    return Tenant(provider="commcare", external_id="synthetic-form-identity")


def _form(name, app_name="", *, repeat=None, leaf="answer"):
    questions = [{"value": f"/data/{leaf}", "type": "Text"}]
    if repeat:
        questions.append({"value": f"{repeat}/{leaf}", "type": "Int", "repeat": repeat})
    return {"name": name, "app_name": app_name, "app_id": f"app-{app_name}", "questions": questions}


def _probe_forms():
    # The issue's probe: labels whose app suffix fell back to unnamed_<digest>.
    forms = {
        f"urn:synthetic:reg:{index}": _form("Reg", app_name, repeat="/data/items")
        for index, app_name in enumerate(["  ", "  ", "", "日本", "日本"])
    }
    forms["urn:synthetic:other"] = _form("Other")
    return forms


def _plan(tenant, forms):
    return sorted(
        (a.name, a.sql_content) for a in generate_system_assets(tenant, {"form_definitions": forms})
    )


def _form_names(tenant, forms):
    xmlns = re.compile(r"WHERE xmlns = '([^']+)'")
    return {
        xmlns.search(a.sql_content).group(1): a.name
        for a in generate_system_assets(tenant, {"form_definitions": forms})
        if "__repeat_" not in a.name
    }


def test_reordered_metadata_generates_identical_models(tenant):
    forms = _probe_forms()
    assert _plan(tenant, forms) == _plan(tenant, dict(reversed(list(forms.items()))))


def test_adding_a_duplicate_does_not_rename_existing_duplicates(tenant):
    forms = {f"urn:synthetic:reg:{i}": _form("Registration", "Clinic") for i in range(2)}
    before = _form_names(tenant, forms)
    after = _form_names(tenant, {"urn:synthetic:reg:new": _form("Registration"), **forms})
    assert {xmlns: after[xmlns] for xmlns in before} == before


def test_duplicates_get_distinct_names_derived_from_xmlns(tenant):
    names = _form_names(tenant, _probe_forms())
    duplicates = [name for xmlns, name in names.items() if xmlns.startswith("urn:synthetic:reg")]
    assert len(set(duplicates)) == 5
    assert all(re.fullmatch(r"stg_form_reg_[0-9a-f]{8}", name) for name in duplicates)
    assert names["urn:synthetic:other"] == "stg_form_other"
    # Changing a duplicate's app label no longer renames it; only xmlns does.
    relabelled = _probe_forms()
    relabelled["urn:synthetic:reg:0"]["app_name"] = "Renamed App"
    assert _form_names(tenant, relabelled) == names


def test_a_duplicate_digest_never_takes_a_literal_form_name(tenant):
    forms = {f"urn:synthetic:reg:{i}": _form("Reg") for i in range(2)}
    names = _form_names(tenant, forms)
    taken = {x: _form(name.removeprefix("stg_form_")) for x, name in names.items()}
    combined = _form_names(tenant, {**forms, **{f"literal:{x}": f for x, f in taken.items()}})
    assert len(set(combined.values())) == len(combined)


def test_every_generated_identifier_fits_postgres_worst_case(tenant):
    long_leaf = "q_" + "very_long_question_identifier_" * 4
    long_repeat = "/data/" + "repeat_group_with_a_very_long_name_" * 3
    forms = {
        f"urn:synthetic:{'x' * 200}:{index}": _form(
            LONG + suffix, app_name=LONG, repeat=long_repeat, leaf=long_leaf
        )
        for index, suffix in enumerate(["", "", "", " A", " A", " B"])
    }
    assets = generate_system_assets(tenant, {"form_definitions": forms})
    names = [a.name for a in assets]
    assert len(names) == len(set(names)) == 12
    for asset in assets:
        assert len(asset.name.encode()) <= PG_MAX_IDENTIFIER_BYTES, asset.name
        for identifier in [*_ALIAS.findall(asset.sql_content), *_REF.findall(asset.sql_content)]:
            assert len(identifier.encode()) <= PG_MAX_IDENTIFIER_BYTES, identifier
        for parent in _REF.findall(asset.sql_content):
            assert parent in names

    # Multi-source workspaces expose each table as <tenant prefix>__<table>.
    prefix = SchemaManager()._view_prefix(
        Tenant(provider="commcare", external_id="x", canonical_name=LONG)
    )
    assert len(prefix) == 32
    views = [view_name(prefix, name) for name in names]
    assert len(set(views)) == len(views)
    assert all(len(view.encode()) <= PG_MAX_IDENTIFIER_BYTES for view in views)


@pytest.mark.django_db
def test_upsert_replaces_legacy_counter_names_and_keeps_repeat_consumers(caplog):
    tenant = Tenant.objects.create(provider="commcare", external_id="synthetic-legacy-forms")
    forms = {
        f"urn:synthetic:reg:{i}": _form("Registration", f"App {i}", repeat="/data/items")
        for i in range(2)
    }
    metadata = TenantMetadata.objects.create(tenant=tenant, metadata={"form_definitions": forms})
    current = generate_system_assets(tenant, metadata.metadata)
    xmlns = re.compile(r"WHERE xmlns = '([^']+)'")
    legacy_parent = {
        "urn:synthetic:reg:0": "stg_form_registration",
        "urn:synthetic:reg:1": "stg_form_registration_app_1_1",
    }
    renames = {
        a.name: legacy_parent[xmlns.search(a.sql_content).group(1)]
        for a in current
        if "__repeat_" not in a.name
    }
    legacy_repeats = {}
    for asset in current:
        if "__repeat_" in asset.name:
            new_parent = _REF.search(asset.sql_content).group(1)
            old_parent = renames[new_parent]
            name = dbt_model_name(f"{old_parent}__repeat_items")
            legacy_repeats[name] = new_parent
            sql = asset.sql_content.replace(f"ref('{new_parent}')", f"ref('{old_parent}')")
        else:
            name, sql = renames[asset.name], asset.sql_content
        TransformationAsset.objects.create(
            tenant=tenant, scope=TransformationScope.SYSTEM, name=name, sql_content=sql
        )

    result = upsert_system_assets(tenant, metadata)

    stored = dict(
        TransformationAsset.objects.filter(tenant=tenant).values_list("name", "sql_content")
    )
    assert result == {"created": 2, "updated": 2, "deleted": 2, "total": 4}
    assert not set(legacy_parent.values()) & set(stored)
    assert set(renames) <= set(stored)
    # Existing repeat models keep their names (and SQL consumers) and now read
    # the renamed parent.
    for name, new_parent in legacy_repeats.items():
        assert f"ref('{new_parent}')" in stored[name]
    for new, old in renames.items():
        assert f"{old} -> {new}" in caplog.text
