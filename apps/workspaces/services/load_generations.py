"""Bounded per-tenant load generations for request coalescing and resume.

A request records, per tenant, the generation it needs published. A writer
holding the tenant lock either reuses a published generation that still passes
a positive equivalence check, resumes a FAILED candidate of the same pending
generation, or starts a new candidate. Only one integer per requested tenant
ever travels through the queue: there is no historical run list and no
cross-host wall-clock comparison.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import pathlib
import uuid
from dataclasses import dataclass
from functools import cache

from asgiref.sync import sync_to_async
from django.conf import settings
from django.db import transaction

from apps.transformations.models import TransformationAsset, TransformationRunStatus
from apps.workspaces.models import (
    MaterializationRun,
    SchemaState,
    TenantLoadGeneration,
    TenantSchema,
    WorkspaceTenant,
)

INTENT_FULL_REFRESH = "full_refresh"
INTENT_RECONCILE_MISSING = "reconcile_missing"
INTENT_KINDS = frozenset({INTENT_FULL_REFRESH, INTENT_RECONCILE_MISSING})

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
# The code whose behaviour decides what a load produces. A deploy that changes
# any of it must not compare equal to a generation loaded before it.
_IMPLEMENTATION_PATHS = (
    "mcp_server/services/materializer.py",
    "mcp_server/loaders",
    "mcp_server/pipeline_registry.py",
    "apps/transformations/services",
    "pipelines",
)


@dataclass(frozen=True)
class ReuseEvidence:
    generation: int
    run: MaterializationRun
    schema: TenantSchema


def implementation_revision() -> str:
    configured = getattr(settings, "SCOUT_IMPLEMENTATION_REVISION", "")
    if configured:
        return str(configured)
    return _source_tree_revision()


@cache
def _source_tree_revision() -> str:
    digest = hashlib.sha256()
    for relative in _IMPLEMENTATION_PATHS:
        path = _REPO_ROOT / relative
        if not path.exists():
            # A renamed path would silently stop tracking code changes, and reuse
            # would start accepting loads made by a different deploy.
            raise RuntimeError(f"Implementation path {relative!r} is missing; update the list")
        files = sorted(p for p in path.rglob("*") if p.is_file()) if path.is_dir() else [path]
        for file in files:
            if "__pycache__" in file.parts:
                continue
            digest.update(str(file.relative_to(_REPO_ROOT)).encode())
            digest.update(file.read_bytes())
    return digest.hexdigest()


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _config_payload(pipeline_config) -> dict:
    if dataclasses.is_dataclass(pipeline_config):
        return dataclasses.asdict(pipeline_config)
    # Registry configs are always dataclasses; this only keys duck-typed test
    # doubles, and never compares equal to a real config.
    return {
        "duck_typed": True,
        **{field: str(getattr(pipeline_config, field, "")) for field in ("name", "version")},
    }


def raw_load_fingerprint(pipeline_config) -> str:
    """Equivalence key for the raw tables a load writes: config and loader code.

    Transform assets are excluded on purpose: a resumed candidate re-runs every
    transform, so only what decides the raw rows has to match.
    """
    payload = {"pipeline": _config_payload(pipeline_config), "revision": implementation_revision()}
    return hashlib.sha256(_canonical(payload).encode()).hexdigest()


def pipeline_fingerprint(pipeline_config, tenant, *, assets=None) -> str:
    """Equivalence key for "the same load": config, transform assets and code."""
    if assets is None:
        assets = TransformationAsset.objects.filter(tenant=tenant)
    asset_snapshot = [
        {
            "name": asset.name,
            "scope": asset.scope,
            "sql": asset.sql_content,
            "tests": asset.test_yaml,
        }
        for asset in sorted(assets, key=lambda asset: (asset.scope, asset.name))
    ]
    payload = {
        "pipeline": _config_payload(pipeline_config),
        "assets": asset_snapshot,
        "revision": implementation_revision(),
    }
    return hashlib.sha256(_canonical(payload).encode()).hexdigest()


def _locked_generation(tenant_id) -> TenantLoadGeneration:
    TenantLoadGeneration.objects.get_or_create(tenant_id=tenant_id)
    return TenantLoadGeneration.objects.select_for_update().get(tenant_id=tenant_id)


def capture_load_intent(tenant_ids, kind: str) -> dict[str, int]:
    """Record, before any wait, the generation each tenant must reach.

    A refresh accepted while a load is pending but not yet started joins it. One
    accepted after that load started fetching asks for the next generation: the
    user clicked after the data was read, so the running load cannot answer it.
    Missing-source reconciliation is satisfied by whatever is already published
    (validated at run time), joins any in-flight first load (even one already
    fetching), and only requests a load when nothing was ever published or pending.
    """
    if kind not in INTENT_KINDS:
        raise ValueError(f"Unknown load intent {kind!r}")
    intent: dict[str, int] = {}
    with transaction.atomic():
        # Sorted, so two requests over overlapping tenants lock rows in one order.
        for tenant_id in sorted({str(tenant_id) for tenant_id in tenant_ids}):
            generation = _locked_generation(tenant_id)
            pending = generation.requested_generation > generation.published_generation
            if kind == INTENT_RECONCILE_MISSING:
                if generation.published_generation >= 1:
                    intent[tenant_id] = generation.published_generation
                    continue
                if pending:
                    # Reconciliation needs data, not freshness: any in-flight load
                    # answers it, even one whose fetch has already started.
                    intent[tenant_id] = generation.requested_generation
                    continue
            elif (
                kind == INTENT_FULL_REFRESH
                and pending
                and generation.loading_generation < generation.requested_generation
            ):
                intent[tenant_id] = generation.requested_generation
                continue
            # Every arm that is satisfied without a new generation continues above.
            generation.requested_generation += 1
            generation.save(update_fields=["requested_generation", "updated_at"])
            intent[tenant_id] = generation.requested_generation
    return intent


async def acapture_workspace_load_intent(workspace_id, kind: str) -> dict[str, int]:
    """Capture a workspace's intent at dispatch time, before the job is queued.

    ``capture_load_intent`` is one atomic write block, so it runs on the
    request's own connection (thread-sensitive) rather than a thread of its own.
    """
    tenant_ids = [
        tenant_id
        async for tenant_id in WorkspaceTenant.objects.filter(
            workspace_id=workspace_id
        ).values_list("tenant_id", flat=True)
    ]
    return await sync_to_async(capture_load_intent)(tenant_ids, kind)


def parse_load_intent(value) -> dict[str, int] | None:
    """Validate an intent carried through task arguments; None when absent/invalid."""
    if not isinstance(value, dict) or not value:
        return None
    intent: dict[str, int] = {}
    for tenant_id, generation in value.items():
        if not isinstance(tenant_id, str) or type(generation) is not int or generation < 1:
            return None
        try:
            canonical = str(uuid.UUID(tenant_id))
        except ValueError:
            return None
        # Normalized, so a braced or bare-hex spelling still matches str(tenant.id);
        # two spellings of one tenant keep the stricter (higher) requirement.
        intent[canonical] = max(generation, intent.get(canonical, 0))
    return intent


_PUBLISHABLE_TRANSFORM_STATUSES = frozenset(
    {TransformationRunStatus.COMPLETED, TransformationRunStatus.TESTS_FAILED}
)


def transforms_publishable(result: dict) -> bool:
    """Whether a run's transforms allow publishing (and so reusing) its schema.

    Decided by the transform run's status, not by whether an error message was
    recorded (an exception can stringify to ""). No transforms at all publishes;
    failed data-quality tests still publish, because the models were built.
    Promotion and reuse share this rule so a published generation is never
    refused for reuse.
    """
    transforms = result.get("transforms")
    if transforms is None or transforms == {}:
        return True
    if not isinstance(transforms, dict):
        return False
    return transforms.get("status") in _PUBLISHABLE_TRANSFORM_STATUSES


def reusable_generation(tenant_id, required: int, fingerprint: str) -> ReuseEvidence | None:
    """Positive equivalence check against the current published generation.

    Reuse needs the published generation to satisfy the request, the same
    fingerprint, a still-ACTIVE promoted schema, and a COMPLETED, publishable
    run (``transforms_publishable``) that is the only non-stale run on that schema (a second run
    means an intervening in-place attempt whose outcome is unknown here).
    """
    generation = (
        TenantLoadGeneration.objects.select_related("published_run", "published_schema")
        .filter(tenant_id=tenant_id)
        .first()
    )
    if generation is None or generation.published_generation < required:
        return None
    if not fingerprint or generation.published_fingerprint != fingerprint:
        return None
    run, schema = generation.published_run, generation.published_schema
    if run is None or schema is None or schema.state != SchemaState.ACTIVE:
        return None
    if run.tenant_schema_id != schema.id or run.state != MaterializationRun.RunState.COMPLETED:
        return None
    result = run.result if isinstance(run.result, dict) else {}
    if result.get("load_fingerprint") != generation.published_fingerprint:
        return None
    if not transforms_publishable(result):
        return None
    other_runs = (
        MaterializationRun.objects.filter(tenant_schema=schema)
        .exclude(id=run.id)
        .exclude(state=MaterializationRun.RunState.STALE)
        .exists()
    )
    if other_runs:
        return None
    return ReuseEvidence(generation=generation.published_generation, run=run, schema=schema)


def begin_load_generation(tenant_id) -> int:
    """Mark the pending generation as loading and return it.

    Called under the tenant lock right before fetching. From now until the load
    ends, a new refresh request asks for the next generation instead of joining.
    Any loading marker already set is stale here (T is exclusive, so its writer
    died), but capture_load_intent reads the marker without T and cannot tell: a
    writer that dies before end_load_generation costs one extra generation, and
    its FAILED candidate is then abandoned rather than resumed. Every exit that
    doesn't publish must therefore call end_load_generation.
    """
    with transaction.atomic():
        generation = _locked_generation(tenant_id)
        if generation.requested_generation <= generation.published_generation:
            generation.requested_generation = generation.published_generation + 1
        generation.loading_generation = generation.requested_generation
        generation.save(update_fields=["requested_generation", "loading_generation", "updated_at"])
        return generation.requested_generation


def end_load_generation(tenant_id, loading_generation: int) -> None:
    """Clear the loading marker after a load that did not publish.

    The generation stays pending, so a retry joins it and can resume its
    candidate rather than being pushed to a fresh generation. A request accepted
    after the failure joins too, and is then answered by that resumed candidate:
    every source is re-fetched on resume (resumable ones from their last cursor),
    so the only staleness is in-place edits behind a cursor, the #187 caveat.
    """
    TenantLoadGeneration.objects.filter(
        tenant_id=tenant_id, loading_generation=loading_generation
    ).update(loading_generation=0)


def resumable_candidate(tenant_id, generation: int, config_fingerprint: str) -> TenantSchema | None:
    """The FAILED candidate a load of ``generation`` may resume, if any.

    Resume keeps per-source progress (the materializer continues resumable
    sources from the candidate's last FAILED/PARTIAL run), so it is only safe
    for the same pending generation with the same raw-load configuration.
    Anything else starts a fresh candidate and leaves this one for cleanup.
    """
    if not config_fingerprint:
        return None
    return (
        TenantSchema.objects.filter(
            tenant_id=tenant_id,
            state=SchemaState.FAILED,
            load_workspace_id__isnull=False,
            load_generation=generation,
            load_config_fingerprint=config_fingerprint,
        )
        # id only makes the tie-break deterministic; it isn't "newest".
        .order_by("-created_at", "-id")
        .first()
    )


def publish_generation(tenant_id, loading_generation: int, run, schema, fingerprint: str) -> None:
    """Record a promoted candidate as the published generation.

    Callers publish inside the promotion transaction (``atomic`` nests) so a
    reader never sees a published generation pointing at a non-ACTIVE schema.
    """
    with transaction.atomic():
        generation = _locked_generation(tenant_id)
        if loading_generation < generation.published_generation:
            # A newer generation is already published; never regress its evidence.
            return
        generation.published_generation = loading_generation
        generation.requested_generation = max(generation.requested_generation, loading_generation)
        if generation.loading_generation == loading_generation:
            generation.loading_generation = 0
        generation.published_run = run
        generation.published_schema = schema
        generation.published_fingerprint = fingerprint
        generation.save(
            update_fields=[
                "published_generation",
                "requested_generation",
                "loading_generation",
                "published_run",
                "published_schema",
                "published_fingerprint",
                "updated_at",
            ]
        )
