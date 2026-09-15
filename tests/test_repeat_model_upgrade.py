"""Rematerialization must preserve the source identity of existing repeat consumers."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from types import SimpleNamespace

import pytest
from django.db import connection

from apps.common.identifiers import fit_identifier
from apps.transformations.models import TransformationAsset, TransformationScope
from apps.transformations.services.commcare_staging import (
    _repeat_base_model_name,
    generate_system_assets,
    upsert_system_assets,
)
from apps.transformations.services.connect_staging import (
    generate_connect_assets,
    upsert_connect_assets,
)
from apps.transformations.services.repeat_identity import RepeatSource, repeat_source
from apps.transformations.services.staging_identity import (
    RepeatModelMigrationRequired,
    StagingModelMigrationRequired,
)
from apps.users.models import Tenant

FALLBACK = "/data/日本語"
LITERAL = "/data/" + fit_identifier("unnamed", unique_key=FALLBACK, always_hash=True)
PATHS = ("/data/left/a", "/data/right/a")


@pytest.fixture(params=["commcare", "commcare_connect"])
def repeat_tenant(request):
    return Tenant(provider=request.param, external_id="synthetic-repeat-upgrade")


def metadata(tenant, paths=PATHS, *, form_name="Registration", extra_question=False):
    questions = [{"value": "/data/date", "type": "Date"}]
    for path in paths:
        questions.append(
            {
                "value": f"{path}/answer",
                "type": "Text",
                "repeat": path if tenant.provider == "commcare" else True,
            }
        )
        if extra_question:
            questions.append(
                {
                    "value": f"{path}/score",
                    "type": "Int",
                    "repeat": path if tenant.provider == "commcare" else True,
                }
            )
    return {
        "form_definitions": {
            "urn:synthetic:registration": {"name": form_name, "questions": questions}
        }
    }


def generate(tenant, data, existing=None):
    if tenant.provider == "commcare":
        return generate_system_assets(tenant, data, existing_assets=existing)
    return generate_connect_assets(data["form_definitions"], tenant, existing_assets=existing)


def upsert(tenant, data):
    action = upsert_system_assets if tenant.provider == "commcare" else upsert_connect_assets
    return action(tenant, SimpleNamespace(metadata=data))


def legacy_assets(tenant, paths=PATHS):
    parent, *repeats = generate(tenant, metadata(tenant, paths))
    surviving = repeats[-1]
    surviving.name = _repeat_base_model_name(parent.name, paths[-1])
    return [parent, surviving]


def source_names(tenant, assets):
    return {
        source.path: asset.name
        for asset in assets
        if (source := repeat_source(asset.sql_content, provider=tenant.provider)) is not None
    }


@pytest.mark.parametrize("paths", [PATHS, (FALLBACK, LITERAL)])
def test_preserves_legacy_source_across_unchanged_reordered_and_extended_metadata(
    repeat_tenant, paths
):
    old = legacy_assets(repeat_tenant, paths)
    old_name, old_id, old_sql = old[-1].name, old[-1].id, old[-1].sql_content
    right_path = tuple(part for part in paths[-1].split("/") if part)
    for current_paths in (paths, tuple(reversed(paths)), (*paths, "/data/third/a")):
        result = generate(repeat_tenant, metadata(repeat_tenant, current_paths), old)
        assert source_names(repeat_tenant, result)[right_path] == old_name
        assert len({asset.name for asset in result}) == len(result)
        assert all(len(asset.name.encode()) <= 63 for asset in result)
        assert (old[-1].id, old[-1].name, old[-1].sql_content) == (old_id, old_name, old_sql)


def test_refreshes_generated_question_projections_without_changing_repeat_identity(repeat_tenant):
    old = legacy_assets(repeat_tenant)
    result = generate(repeat_tenant, metadata(repeat_tenant, extra_question=True), old)
    preserved = next(asset for asset in result if asset.name == old[-1].name)
    assert "::integer" in preserved.sql_content
    again = generate(repeat_tenant, metadata(repeat_tenant), result)
    assert source_names(repeat_tenant, again) == source_names(repeat_tenant, result)
    assert (
        "::integer" not in next(asset for asset in again if asset.name == old[-1].name).sql_content
    )


def test_reserves_persisted_digest_names_before_a_new_literal_source(repeat_tenant):
    old = generate(repeat_tenant, metadata(repeat_tenant), [])
    names = source_names(repeat_tenant, old)
    preserved_name = names[("data", "left", "a")]
    leaf = preserved_name.split("__repeat_", 1)[1]
    paths = (*PATHS, f"/data/{leaf}")
    result = generate(repeat_tenant, metadata(repeat_tenant, paths), old)
    upgraded = source_names(repeat_tenant, result)
    assert all(upgraded[path] == name for path, name in names.items())
    assert upgraded[("data", leaf)] != preserved_name
    reverse = generate(repeat_tenant, metadata(repeat_tenant, tuple(reversed(paths))), old)
    assert source_names(repeat_tenant, reverse) == upgraded
    assert (
        source_names(repeat_tenant, generate(repeat_tenant, metadata(repeat_tenant, paths), result))
        == upgraded
    )


def test_true_source_removal_keeps_orphan_cleanup_but_does_not_reassign_its_name(repeat_tenant):
    old = legacy_assets(repeat_tenant)
    result = generate(repeat_tenant, metadata(repeat_tenant, ["/data/left/a"]), old)
    assert old[-1].name not in {asset.name for asset in result}
    assert set(source_names(repeat_tenant, result)) == {("data", "left", "a")}
    empty = {"form_definitions": {}}
    assert not source_names(repeat_tenant, generate(repeat_tenant, empty, old))


def test_can_follow_a_uniquely_proven_renamed_parent_without_renaming_the_repeat():
    tenant = Tenant(provider="commcare", external_id="synthetic-parent-rename")
    old = legacy_assets(tenant)
    result = generate(tenant, metadata(tenant, form_name="New registration label"), old)
    preserved = next(asset for asset in result if asset.name == old[-1].name)
    assert "ref('stg_form_new_registration_label')" in preserved.sql_content
    assert source_names(tenant, result)[("data", "right", "a")] == old[-1].name


def test_duplicate_existing_owners_require_migration(repeat_tenant):
    old = legacy_assets(repeat_tenant)
    duplicate = deepcopy(old[-1])
    duplicate.name += "_second"
    with pytest.raises(RepeatModelMigrationRequired, match="multiple existing models"):
        generate(repeat_tenant, metadata(repeat_tenant), [*old, duplicate])


@pytest.mark.parametrize("question", ["jsonb_array_elements", "jsonb_array_elements(form_json)"])
def test_nonrepeat_question_named_like_a_repeat_function_is_not_a_migration(
    repeat_tenant, question
):
    data = metadata(repeat_tenant, [])
    data["form_definitions"]["urn:synthetic:registration"]["questions"] = [
        {"value": f"/data/{question}", "type": "Text"}
    ]
    old = generate(repeat_tenant, data)
    result = generate(repeat_tenant, data, old)
    assert [(asset.name, asset.sql_content) for asset in result] == [
        (asset.name, asset.sql_content) for asset in old
    ]


@pytest.mark.parametrize(
    "suffix", [" LIMIT 1", " -- edited query", "; SELECT 1", " AND (", "; SELECT (", " /*"]
)
def test_renamed_noncanonical_repeat_query_is_not_silently_swept(repeat_tenant, suffix):
    old = legacy_assets(repeat_tenant)
    old[-1].name = "renamed_system_query"
    old[-1].description = "Edited system query"
    old[-1].sql_content += suffix
    with pytest.raises(RepeatModelMigrationRequired, match="source cannot be proven"):
        generate(repeat_tenant, metadata(repeat_tenant), old)


def test_renamed_repeat_replaced_with_unrelated_sql_is_not_swept(repeat_tenant):
    old = legacy_assets(repeat_tenant)
    old[-1].name = "renamed_system_query"
    old[-1].description = "Edited system query"
    old[-1].sql_content = "SELECT 1"
    with pytest.raises(RepeatModelMigrationRequired, match="source cannot be proven"):
        generate(repeat_tenant, metadata(repeat_tenant), old)


@pytest.mark.parametrize("sql", ["SELECT 1", "SELECT ("])
def test_unproven_repeat_at_the_current_preferred_name_is_not_overwritten(repeat_tenant, sql):
    old = generate(repeat_tenant, metadata(repeat_tenant))
    old[-1].sql_content = sql
    with pytest.raises(RepeatModelMigrationRequired, match="source cannot be proven"):
        generate(repeat_tenant, metadata(repeat_tenant), old)


@pytest.mark.parametrize("sql", ["SELECT 1", "SELECT ("])
def test_unproven_nonrepeat_system_orphan_requires_migration(repeat_tenant, sql):
    data = metadata(repeat_tenant, [])
    old = generate(repeat_tenant, data)
    old.append(
        TransformationAsset(
            tenant=repeat_tenant,
            scope=TransformationScope.SYSTEM,
            name="unclassified_source",
            sql_content=sql,
        )
    )
    with pytest.raises(
        RepeatModelMigrationRequired, match="staging model 'unclassified_source': its source"
    ) as error:
        generate(repeat_tenant, data, old)
    assert "No staging assets, dependent models, or replaces links were changed" in str(error.value)


@pytest.mark.parametrize("sql", ["SELECT 1", "SELECT ("])
def test_present_nonrepeat_system_models_can_still_be_regenerated(repeat_tenant, sql):
    data = metadata(repeat_tenant, [])
    old = generate(repeat_tenant, data)
    expected = old[0].sql_content
    old[0].sql_content = sql
    result = generate(repeat_tenant, data, old)
    assert [(asset.name, asset.sql_content) for asset in result] == [(old[0].name, expected)]


def test_canonical_form_or_connect_parent_can_be_removed(repeat_tenant):
    old = generate(repeat_tenant, metadata(repeat_tenant, []))
    if repeat_tenant.provider == "commcare_connect":
        # Connect always generates stg_visits; a retired canonical parent may
        # still be removed without mistaking its typed projections for custom SQL.
        old[0].name = "retired_visits"
    result = generate(repeat_tenant, {"form_definitions": {}}, old)
    assert old[0].name not in {asset.name for asset in result}


def case_assets(tenant):
    return generate_system_assets(
        tenant,
        {
            "case_types": [{"name": "patient"}],
            "app_definitions": [
                {
                    "modules": [
                        {
                            "case_type": "patient",
                            "case_properties": [
                                "closed",
                                "mother's_name",
                                "jsonb_array_elements(form_json)",
                            ],
                        }
                    ]
                }
            ],
        },
    )


def test_canonical_case_removal_accepts_quoted_keys_and_reserved_aliases():
    tenant = Tenant(provider="commcare", external_id="synthetic-case-removal")
    old = case_assets(tenant)
    assert 'AS "closed_2"' in old[0].sql_content
    assert "mother''s_name" in old[0].sql_content
    assert generate_system_assets(tenant, {}, existing_assets=old) == []


@pytest.mark.parametrize(
    "edit",
    [
        lambda sql: sql + " -- edited case",
        lambda sql: sql + " LIMIT 1",
        lambda sql: sql + " AND closed",
        lambda sql: sql.replace("FROM raw_cases", "FROM edited_cases"),
        lambda sql: sql.replace("properties->>", "properties::jsonb->>"),
        lambda sql: sql.replace("properties->>'closed'", "upper(properties->>'closed')"),
    ],
)
def test_noncanonical_removed_case_requires_migration(edit):
    tenant = Tenant(provider="commcare", external_id="synthetic-case-removal")
    old = case_assets(tenant)
    changed = edit(old[0].sql_content)
    assert changed != old[0].sql_content
    old[0].sql_content = changed
    with pytest.raises(RepeatModelMigrationRequired, match="source cannot be proven"):
        generate_system_assets(tenant, {}, existing_assets=old)


@pytest.mark.parametrize(
    "edit",
    [
        lambda sql: sql + " AND elem.ordinality > 1",
        lambda sql: sql + " LIMIT 1",
        lambda sql: sql + "; SELECT 1",
        lambda sql: sql + " -- edited query",
        lambda sql: sql.replace("elem.value->>'answer'", "upper(elem.value->>'answer')"),
        lambda sql: sql.replace("ORDER BY elem.ordinality", "ORDER BY elem.value"),
        lambda sql: sql.replace(" f,\n", " f JOIN extra_source extra ON true,\n"),
        lambda sql: sql.replace("jsonb_array_elements(", "jsonb_array_elements_text("),
        lambda sql: sql.replace("IS NOT NULL", "IS NULL"),
    ],
)
def test_noncanonical_repeat_sql_never_selects_a_source(repeat_tenant, edit):
    old = legacy_assets(repeat_tenant)
    old[-1].sql_content = edit(old[-1].sql_content)
    before = [(asset.id, asset.name, asset.sql_content) for asset in old]
    with pytest.raises(RepeatModelMigrationRequired, match="source cannot be proven"):
        generate(repeat_tenant, metadata(repeat_tenant), old)
    assert [(asset.id, asset.name, asset.sql_content) for asset in old] == before


@pytest.mark.parametrize(
    "edit",
    [
        lambda sql: sql + " LIMIT 1",
        lambda sql: sql.replace("FROM raw_", "FROM edited_raw_"),
        lambda sql: sql.replace("form_data,", "NULL::jsonb AS form_data,").replace(
            "form_json,", "NULL::jsonb AS form_json,"
        ),
    ],
)
def test_noncanonical_parent_cannot_establish_identity(repeat_tenant, edit):
    old = legacy_assets(repeat_tenant)
    changed = edit(old[0].sql_content)
    assert changed != old[0].sql_content
    old[0].sql_content = changed
    with pytest.raises(RepeatModelMigrationRequired, match="original parent source"):
        generate(repeat_tenant, metadata(repeat_tenant), old)


def test_parent_filter_reassignment_is_not_treated_as_a_source_removal():
    tenant = Tenant(provider="commcare", external_id="synthetic-parent-filter")
    old = legacy_assets(tenant)
    old[0].sql_content = old[0].sql_content.replace(
        "urn:synthetic:registration", "urn:synthetic:other"
    )
    with pytest.raises(
        RepeatModelMigrationRequired, match="parent model now identifies a different source"
    ):
        generate(tenant, metadata(tenant), old)


@pytest.mark.parametrize(
    "path", ["/data/O'Brien/a,b", "/data/{a}/日本語", "/data/x/123", "/data/" + "x" * 100]
)
@pytest.mark.parametrize("question_type", ["Text", "Int", "Double", "Decimal", "Date", "DateTime"])
def test_recognizes_actual_generator_paths_and_casts(repeat_tenant, path, question_type):
    data = metadata(repeat_tenant, [path])
    data["form_definitions"]["urn:synthetic:registration"]["questions"][-1]["type"] = question_type
    parent, repeat = generate(repeat_tenant, data)
    assert repeat_source(repeat.sql_content, provider=repeat_tenant.provider) == RepeatSource(
        parent.name, tuple(part for part in path.split("/") if part)
    )
    result = generate(repeat_tenant, data, [parent, repeat])
    assert result[-1].name == repeat.name


def test_repeat_migration_has_the_shared_materialization_error_contract():
    error = RepeatModelMigrationRequired("synthetic failure")
    assert isinstance(error, StagingModelMigrationRequired)
    assert isinstance(error, ValueError)
    assert error.code == "SCHEMA_BUILD_FAILED"


def save_legacy_fixture(tenant, paths=PATHS):
    tenant.save()
    old = legacy_assets(tenant, paths)
    for asset in old:
        asset.save()
    custom = TransformationAsset.objects.create(
        tenant=tenant,
        scope=TransformationScope.TENANT,
        name="custom_repeat",
        sql_content=f"SELECT * FROM {old[-1].name}",
        replaces=old[-1],
    )
    return old, custom


@pytest.mark.django_db
@pytest.mark.parametrize("paths", [PATHS, (FALLBACK, LITERAL)])
def test_orm_upgrade_preserves_primary_key_replaces_and_custom_sql(repeat_tenant, paths):
    old, custom = save_legacy_fixture(repeat_tenant, paths)
    legacy_id, legacy_name, consumer_sql = old[-1].id, old[-1].name, custom.sql_content
    for current_paths in (paths, tuple(reversed(paths)), (*paths, "/data/new/a")):
        result = upsert(repeat_tenant, metadata(repeat_tenant, current_paths, extra_question=True))
        assert result["deleted"] == 0
        assert result["total"] == len(current_paths) + 1
        preserved = TransformationAsset.objects.get(pk=legacy_id)
        assert preserved.name == legacy_name
        assert repeat_source(preserved.sql_content, provider=repeat_tenant.provider).path == tuple(
            part for part in paths[-1].split("/") if part
        )
        custom.refresh_from_db()
        assert custom.replaces_id == legacy_id
        assert custom.sql_content == consumer_sql
    again = upsert(repeat_tenant, metadata(repeat_tenant, current_paths, extra_question=True))
    assert again["created"] == again["deleted"] == 0


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("suffix", "renamed"),
    [
        (" AND elem.ordinality > 1", False),
        (" -- edited query", True),
        ("; SELECT 1", True),
        (" AND (", True),
        ("; SELECT (", True),
        (" /*", True),
        (None, True),
    ],
)
def test_orm_ambiguity_fails_before_any_asset_write(repeat_tenant, mocker, suffix, renamed):
    old, custom = save_legacy_fixture(repeat_tenant)
    old[-1].sql_content = old[-1].sql_content + suffix if suffix is not None else "SELECT 1"
    if renamed:
        old[-1].name = "renamed_system_query"
        old[-1].description = "Edited system query"
    old[-1].save()
    before = list(
        TransformationAsset.objects.values_list("id", "name", "sql_content", "replaces_id")
    )
    writes = mocker.spy(TransformationAsset.objects, "update_or_create")
    with pytest.raises(RepeatModelMigrationRequired, match="explicit migration"):
        upsert(repeat_tenant, metadata(repeat_tenant))
    writes.assert_not_called()
    assert (
        list(TransformationAsset.objects.values_list("id", "name", "sql_content", "replaces_id"))
        == before
    )
    custom.refresh_from_db()
    assert custom.replaces_id == old[-1].id


@pytest.mark.django_db
def test_orm_write_failure_rolls_back_preserved_asset_updates(repeat_tenant, mocker):
    save_legacy_fixture(repeat_tenant)
    before = list(
        TransformationAsset.objects.values_list("id", "name", "sql_content", "replaces_id")
    )
    original = TransformationAsset.objects.update_or_create
    count = 0

    def fail_second_write(**kwargs):
        nonlocal count
        count += 1
        if count == 2:
            raise RuntimeError("synthetic write failure")
        return original(**kwargs)

    mocker.patch.object(
        TransformationAsset.objects, "update_or_create", side_effect=fail_second_write
    )
    with pytest.raises(RuntimeError, match="synthetic write failure"):
        upsert(repeat_tenant, metadata(repeat_tenant, extra_question=True))
    assert count == 2
    assert (
        list(TransformationAsset.objects.values_list("id", "name", "sql_content", "replaces_id"))
        == before
    )


@pytest.mark.django_db
def test_orm_true_removed_source_is_swept_without_retargeting_its_name(repeat_tenant):
    old, custom = save_legacy_fixture(repeat_tenant)
    result = upsert(repeat_tenant, metadata(repeat_tenant, ["/data/left/a"]))
    assert result["deleted"] == 1
    assert not TransformationAsset.objects.filter(pk=old[-1].id).exists()
    assert not TransformationAsset.objects.filter(tenant=repeat_tenant, name=old[-1].name).exists()
    custom.refresh_from_db()
    assert custom.replaces_id is None
    assert custom.sql_content == f"SELECT * FROM {old[-1].name}"


@pytest.mark.django_db
def test_orm_preservation_is_scoped_to_the_tenant_and_system_assets(repeat_tenant):
    old, _custom = save_legacy_fixture(repeat_tenant)
    other = Tenant(provider=repeat_tenant.provider, external_id="synthetic-other-repeat")
    other_assets, other_custom = save_legacy_fixture(other)
    other_assets[-1].sql_content += " AND elem.ordinality > 1"
    other_assets[-1].save()
    tenant_override = TransformationAsset.objects.create(
        tenant=repeat_tenant,
        scope=TransformationScope.TENANT,
        name=old[-1].name,
        sql_content="SELECT 'custom query' AS answer",
    )
    untouched_ids = [asset.id for asset in other_assets] + [other_custom.id, tenant_override.id]
    before = list(
        TransformationAsset.objects.filter(id__in=untouched_ids).values_list(
            "id", "name", "sql_content", "replaces_id", "updated_at"
        )
    )
    assert upsert(repeat_tenant, metadata(repeat_tenant))["deleted"] == 0
    assert (
        list(
            TransformationAsset.objects.filter(id__in=untouched_ids).values_list(
                "id", "name", "sql_content", "replaces_id", "updated_at"
            )
        )
        == before
    )


@pytest.mark.django_db(transaction=True)
def test_orm_concurrent_upserts_plan_against_the_previous_committed_identity(repeat_tenant, mocker):
    old, custom = save_legacy_fixture(repeat_tenant)
    first_planned = threading.Event()
    release_first = threading.Event()
    second_started = threading.Event()
    worker = threading.local()
    snapshots = {}
    second_backend = []
    if repeat_tenant.provider == "commcare":
        generator = generate_system_assets
        target = "apps.transformations.services.commcare_staging.generate_system_assets"
    else:
        generator = generate_connect_assets
        target = "apps.transformations.services.connect_staging.generate_connect_assets"

    def plan(*args, **kwargs):
        snapshots[worker.name] = {asset.id: asset.name for asset in kwargs["existing_assets"]}
        if worker.name == "first":
            first_planned.set()
            assert release_first.wait(10), "First upsert was never released"
        return generator(*args, **kwargs)

    def run(name):
        worker.name = name
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout = '5s'")
                if name == "second":
                    cursor.execute("SELECT pg_backend_pid()")
                    second_backend.append(cursor.fetchone()[0])
                    second_started.set()
            paths = PATHS if name == "first" else tuple(reversed(PATHS))
            return upsert(repeat_tenant, metadata(repeat_tenant, paths))
        finally:
            connection.close()

    mocker.patch(target, side_effect=plan)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(run, "first")
        try:
            assert first_planned.wait(10)
            second = executor.submit(run, "second")
            assert second_started.wait(10)
            deadline = time.monotonic() + 5
            while True:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT wait_event_type FROM pg_stat_activity WHERE pid = %s",
                        [second_backend[0]],
                    )
                    row = cursor.fetchone()
                if row and row[0] == "Lock":
                    break
                assert not second.done(), "Second upsert did not wait for the tenant lock"
                assert time.monotonic() < deadline, "Second upsert never waited for serialization"
                time.sleep(0.01)
            assert "second" not in snapshots
        finally:
            release_first.set()
        assert first.result(timeout=10)["created"] == 1
        assert second.result(timeout=10)["created"] == 0

    assert len(snapshots["first"]) == 2
    assert len(snapshots["second"]) == 3
    assert snapshots["second"][old[-1].id] == old[-1].name
    custom.refresh_from_db()
    assert custom.replaces_id == old[-1].id
