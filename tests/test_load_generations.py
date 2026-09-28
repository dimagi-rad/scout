"""Generation intent, join/bump rules, positive reuse evidence and resume eligibility."""

import dataclasses
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone

from apps.transformations.models import TransformationAsset, TransformationScope
from apps.users.models import Tenant
from apps.workspaces.models import (
    MaterializationRun,
    SchemaState,
    TenantLoadGeneration,
    TenantSchema,
)
from apps.workspaces.services import load_generations
from apps.workspaces.services.load_generations import (
    INTENT_FULL_REFRESH,
    INTENT_RECONCILE_MISSING,
    begin_load_generation,
    capture_load_intent,
    end_load_generation,
    parse_load_intent,
    pipeline_fingerprint,
    publish_generation,
    raw_load_fingerprint,
    resumable_candidate,
    reusable_generation,
    transforms_publishable,
)
from mcp_server.pipeline_registry import get_registry

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def pipeline():
    return get_registry().get_by_provider("commcare")


def _published(tenant, pipeline, *, generation=1, state=SchemaState.ACTIVE, result=None):
    schema = TenantSchema.objects.create(
        tenant=tenant, schema_name=f"gen_{tenant.id.hex[:8]}_{generation}", state=state
    )
    run = MaterializationRun.objects.create(
        tenant_schema=schema,
        pipeline=pipeline.name,
        state=MaterializationRun.RunState.COMPLETED,
        result=result if result is not None else {"sources": {}, "pipeline": pipeline.name},
    )
    fingerprint = pipeline_fingerprint(pipeline, tenant)
    run.result["load_fingerprint"] = fingerprint
    run.save(update_fields=["result"])
    publish_generation(tenant.id, generation, run, schema, fingerprint)
    return schema, run, fingerprint


def test_full_refresh_bumps_then_joins_then_bumps_again(tenant, pipeline):
    first = capture_load_intent([tenant.id], INTENT_FULL_REFRESH)
    second = capture_load_intent([tenant.id], INTENT_FULL_REFRESH)
    assert first == second == {str(tenant.id): 1}

    loading = begin_load_generation(tenant.id)
    assert loading == 1
    _published(tenant, pipeline, generation=loading)

    later = capture_load_intent([tenant.id], INTENT_FULL_REFRESH)
    assert later == {str(tenant.id): 2}
    row = TenantLoadGeneration.objects.get(tenant=tenant)
    assert (row.requested_generation, row.published_generation) == (2, 1)


def test_reconcile_missing_is_satisfied_by_published_generation(tenant, pipeline):
    assert capture_load_intent([tenant.id], INTENT_RECONCILE_MISSING) == {str(tenant.id): 1}
    _published(tenant, pipeline, generation=1)
    assert capture_load_intent([tenant.id], INTENT_RECONCILE_MISSING) == {str(tenant.id): 1}
    assert capture_load_intent([tenant.id], INTENT_FULL_REFRESH) == {str(tenant.id): 2}
    # A refresh is pending: reconciliation still only needs what is published.
    assert capture_load_intent([tenant.id], INTENT_RECONCILE_MISSING) == {str(tenant.id): 1}


def test_requests_join_a_pending_load_only_until_it_starts_fetching(tenant, pipeline):
    _published(tenant, pipeline, generation=1)
    assert capture_load_intent([tenant.id], INTENT_FULL_REFRESH) == {str(tenant.id): 2}
    assert capture_load_intent([tenant.id], INTENT_FULL_REFRESH) == {str(tenant.id): 2}

    assert begin_load_generation(tenant.id) == 2
    # Clicked after the fetch started: generation 2 read its data too early.
    assert capture_load_intent([tenant.id], INTENT_FULL_REFRESH) == {str(tenant.id): 3}
    assert capture_load_intent([tenant.id], INTENT_FULL_REFRESH) == {str(tenant.id): 3}


def test_a_failed_load_stays_joinable_so_a_retry_can_resume_it(tenant, pipeline):
    _published(tenant, pipeline, generation=1)
    loading = begin_load_generation(tenant.id)
    end_load_generation(tenant.id, loading)

    assert capture_load_intent([tenant.id], INTENT_FULL_REFRESH) == {str(tenant.id): loading}
    assert begin_load_generation(tenant.id) == loading


def test_an_older_generation_never_overwrites_newer_published_evidence(tenant, pipeline):
    newer_schema, newer_run, newer_fingerprint = _published(tenant, pipeline, generation=3)
    stale = TenantSchema.objects.create(
        tenant=tenant, schema_name="gen_stale", state=SchemaState.ACTIVE
    )
    stale_run = MaterializationRun.objects.create(
        tenant_schema=stale, pipeline=pipeline.name, state=MaterializationRun.RunState.COMPLETED
    )

    publish_generation(tenant.id, 2, stale_run, stale, "stale-fingerprint")

    row = TenantLoadGeneration.objects.get(tenant=tenant)
    assert row.published_generation == 3
    assert (row.published_run_id, row.published_schema_id) == (newer_run.id, newer_schema.id)
    assert row.published_fingerprint == newer_fingerprint


def test_intent_is_one_bounded_integer_per_tenant():
    a = Tenant.objects.create(provider="commcare", external_id="a", canonical_name="A")
    b = Tenant.objects.create(provider="ocs", external_id="b", canonical_name="B")
    intent = capture_load_intent([b.id, a.id, str(a.id)], INTENT_FULL_REFRESH)
    assert intent == {str(a.id): 1, str(b.id): 1}
    assert parse_load_intent(intent) == intent
    assert parse_load_intent({}) is None
    assert parse_load_intent({str(a.id): 0}) is None
    assert parse_load_intent({str(a.id): "1"}) is None
    assert parse_load_intent({"not-a-uuid": 1}) is None
    with pytest.raises(ValueError):
        capture_load_intent([a.id], "bogus")


def test_reuse_requires_positive_equivalence(tenant, pipeline):
    schema, run, fingerprint = _published(tenant, pipeline, generation=3)
    evidence = reusable_generation(tenant.id, 3, fingerprint)
    assert evidence is not None and evidence.schema == schema and evidence.run == run
    assert reusable_generation(tenant.id, 4, fingerprint) is None
    assert reusable_generation(tenant.id, 3, "other-fingerprint") is None


@pytest.mark.parametrize(
    "spoil",
    [
        "schema_teardown",
        "run_failed",
        "extra_run",
        "transform_error",
        "run_missing",
        "missing_receipt",
        "wrong_receipt",
    ],
)
def test_reuse_rejects_stale_partial_or_superseded_evidence(tenant, pipeline, spoil):
    schema, run, fingerprint = _published(tenant, pipeline, generation=1)
    if spoil == "schema_teardown":
        TenantSchema.objects.filter(id=schema.id).update(state=SchemaState.TEARDOWN)
    elif spoil == "run_failed":
        MaterializationRun.objects.filter(id=run.id).update(
            state=MaterializationRun.RunState.FAILED
        )
    elif spoil == "extra_run":
        MaterializationRun.objects.create(
            tenant_schema=schema, pipeline=pipeline.name, state=MaterializationRun.RunState.PARTIAL
        )
    elif spoil == "transform_error":
        # Keep the receipt intact so only the transform error can refuse reuse.
        run.result["transforms"] = {"status": "failed", "error": "dbt failed"}
        run.save(update_fields=["result"])
    elif spoil == "run_missing":
        run.delete()
    elif spoil in {"missing_receipt", "wrong_receipt"}:
        run.result.pop("load_fingerprint")
        if spoil == "wrong_receipt":
            run.result["load_fingerprint"] = "different-execution"
        run.save(update_fields=["result"])
    assert reusable_generation(tenant.id, 1, fingerprint) is None


def test_stale_runs_do_not_block_reuse(tenant, pipeline):
    schema, _run, fingerprint = _published(tenant, pipeline, generation=1)
    MaterializationRun.objects.create(
        tenant_schema=schema, pipeline=pipeline.name, state=MaterializationRun.RunState.STALE
    )
    assert reusable_generation(tenant.id, 1, fingerprint) is not None


def test_legacy_tenant_without_generation_is_never_reused(tenant, pipeline):
    TenantSchema.objects.create(
        tenant=tenant, schema_name="legacy_active", state=SchemaState.ACTIVE
    )
    assert reusable_generation(tenant.id, 1, pipeline_fingerprint(pipeline, tenant)) is None


def test_fingerprint_covers_config_assets_and_implementation(tenant, pipeline):
    base = pipeline_fingerprint(pipeline, tenant)
    assert base == pipeline_fingerprint(pipeline, tenant)
    TransformationAsset.objects.create(
        tenant=tenant, name="stg_extra", scope=TransformationScope.TENANT, sql_content="select 1"
    )
    with_asset = pipeline_fingerprint(pipeline, tenant)
    assert with_asset != base
    with patch.object(load_generations, "implementation_revision", return_value="deploy-b"):
        assert pipeline_fingerprint(pipeline, tenant) != with_asset
    other = get_registry().get_by_provider("ocs")
    assert pipeline_fingerprint(other, tenant) != with_asset


def test_raw_load_fingerprint_ignores_assets_but_not_config_or_code(tenant, pipeline):
    base = raw_load_fingerprint(pipeline)
    TransformationAsset.objects.create(
        tenant=tenant, name="stg_extra", scope=TransformationScope.TENANT, sql_content="select 1"
    )
    # Transforms re-run in full on resume, so assets don't decide raw rows.
    assert raw_load_fingerprint(pipeline) == base
    with patch.object(load_generations, "implementation_revision", return_value="deploy-b"):
        assert raw_load_fingerprint(pipeline) != base
    assert raw_load_fingerprint(dataclasses.replace(pipeline, version="next")) != base


def test_a_configured_revision_is_read_every_call(settings, pipeline):
    settings.SCOUT_IMPLEMENTATION_REVISION = "deploy-a"
    first = raw_load_fingerprint(pipeline)
    settings.SCOUT_IMPLEMENTATION_REVISION = "deploy-b"

    assert raw_load_fingerprint(pipeline) != first


def test_a_missing_implementation_path_fails_loudly(monkeypatch):
    load_generations._source_tree_revision.cache_clear()
    monkeypatch.setattr(load_generations, "_IMPLEMENTATION_PATHS", ("no/such/path",))
    try:
        with pytest.raises(RuntimeError, match="no/such/path"):
            load_generations._source_tree_revision()
    finally:
        load_generations._source_tree_revision.cache_clear()


def _failed_candidate(tenant, *, generation, config, suffix="a", created_at=None):
    schema = TenantSchema.objects.create(
        tenant=tenant,
        schema_name=f"cand_{tenant.id.hex[:8]}_{suffix}",
        state=SchemaState.FAILED,
        load_workspace_id=tenant.id,
        load_generation=generation,
        load_config_fingerprint=config,
    )
    if created_at is not None:
        TenantSchema.objects.filter(pk=schema.pk).update(created_at=created_at)
    return schema


def test_resume_only_the_same_pending_generation_with_matching_config(tenant, pipeline):
    config = raw_load_fingerprint(pipeline)
    candidate = _failed_candidate(tenant, generation=2, config=config)

    assert resumable_candidate(tenant.id, 2, config) == candidate
    assert resumable_candidate(tenant.id, 3, config) is None
    assert resumable_candidate(tenant.id, 2, "changed-config") is None
    assert resumable_candidate(tenant.id, 2, "") is None


def test_resume_never_picks_a_refresh_request_or_live_candidate(tenant, pipeline):
    config = raw_load_fingerprint(pipeline)
    TenantSchema.objects.create(
        tenant=tenant,
        schema_name=f"refresh_{tenant.id.hex[:8]}",
        state=SchemaState.FAILED,
        load_generation=2,
        load_config_fingerprint=config,
    )
    TenantSchema.objects.create(
        tenant=tenant,
        schema_name=f"live_{tenant.id.hex[:8]}",
        state=SchemaState.PROVISIONING,
        load_workspace_id=tenant.id,
        load_generation=2,
        load_config_fingerprint=config,
    )

    assert resumable_candidate(tenant.id, 2, config) is None


def test_resume_prefers_the_newest_matching_candidate(tenant, pipeline):
    config = raw_load_fingerprint(pipeline)
    now = timezone.now()
    _failed_candidate(tenant, generation=2, config=config, suffix="old", created_at=now)
    newest = _failed_candidate(
        tenant, generation=2, config=config, suffix="new", created_at=now + timedelta(seconds=1)
    )

    assert resumable_candidate(tenant.id, 2, config) == newest


def test_reconcile_missing_joins_a_first_load_already_fetching(tenant):
    assert capture_load_intent([tenant.id], INTENT_FULL_REFRESH) == {str(tenant.id): 1}
    assert begin_load_generation(tenant.id) == 1

    # Nothing is published yet, but the running load will answer reconciliation.
    assert capture_load_intent([tenant.id], INTENT_RECONCILE_MISSING) == {str(tenant.id): 1}
    assert TenantLoadGeneration.objects.get(tenant=tenant).requested_generation == 1


def test_intent_keys_are_normalized_tenant_uuids(tenant):
    braced = "{" + str(tenant.id).upper() + "}"
    assert parse_load_intent({braced: 1}) == {str(tenant.id): 1}


def test_two_spellings_of_one_tenant_keep_the_stricter_requirement(tenant):
    braced = "{" + str(tenant.id).upper() + "}"
    assert parse_load_intent({str(tenant.id): 3, braced: 1}) == {str(tenant.id): 3}
    assert parse_load_intent({braced: 1, str(tenant.id): 3}) == {str(tenant.id): 3}


@pytest.mark.parametrize(
    ("transforms", "publishable"),
    [
        (None, True),
        ({}, True),
        ({"status": "completed"}, True),
        ({"status": "tests_failed", "error": "quality assertion"}, True),
        # An exception can stringify to "": the status, not the message, decides.
        ({"status": "failed"}, False),
        ({"status": "failed", "error": ""}, False),
        ({"error": "transform phase raised"}, False),
        ("not a dict", False),
    ],
)
def test_transform_publishability_is_decided_by_status(transforms, publishable):
    result = {"sources": {}} if transforms is None else {"sources": {}, "transforms": transforms}
    assert transforms_publishable(result) is publishable
