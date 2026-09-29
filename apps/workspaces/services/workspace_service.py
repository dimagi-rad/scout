"""Service functions for workspace tenant management."""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.workspaces import access_cache
from apps.workspaces.models import (
    SchemaState,
    TenantSchema,
    WorkspaceTenant,
    WorkspaceViewSchema,
)
from apps.workspaces.services.load_generations import (
    INTENT_RECONCILE_MISSING,
    capture_load_intent,
)
from apps.workspaces.services.schema_manager import RETIRED_VIEW_STATES
from apps.workspaces.services.tenant_coverage import coverage_entry, parse_coverage
from apps.workspaces.tasks import (
    materialize_workspace,
    rebuild_workspace_view_schema,
    teardown_view_schema_task,
)


def _invalidate_on_commit(workspace) -> None:
    # Not before commit: a resolution on another connection would read the old
    # tenant set under the new generation and cache it.
    workspace_id = workspace.id
    transaction.on_commit(lambda: access_cache.invalidate(workspace_id=workspace_id))


def add_workspace_tenant(workspace, tenant, *, actor_id=None) -> tuple[WorkspaceTenant, bool]:
    """Add a tenant to a workspace and publish it once it has data.

    A tenant that already serves data (loaded for a sibling workspace) only needs
    this workspace's views rebuilt. One with nothing loaded is loaded as
    ``actor_id``, and that load republishes the views and Cube with it. Until then
    the views are rebuilt without it but stay ACTIVE (not provisioning, which
    would take every source's data tools offline for the whole load). The new
    source is recorded under the ACTIVE views' ``excluded_tenants`` in this
    transaction, and the rebuild keeps it there, so coverage honestly reports it
    missing from the moment it is added instead of the views silently omitting
    it. Uses get_or_create to handle concurrent requests; only a newly created
    link dispatches work.

    Returns (WorkspaceTenant, created) where created is False if the tenant
    was already in the workspace.
    """
    with transaction.atomic():
        wt, created = WorkspaceTenant.objects.get_or_create(workspace=workspace, tenant=tenant)
        if created:
            _invalidate_on_commit(workspace)
            serving = TenantSchema.objects.filter(tenant=tenant, state=SchemaState.ACTIVE).exists()
            if serving or actor_id is None:
                WorkspaceViewSchema.objects.filter(workspace=workspace).update(
                    state=SchemaState.PROVISIONING
                )
                rebuild_workspace_view_schema.defer(workspace_id=str(workspace.id))
            else:
                # A retired row serves nothing, and the rebuild below skips it
                # unless it is marked as wanted again.
                WorkspaceViewSchema.objects.filter(
                    workspace=workspace, state__in=RETIRED_VIEW_STATES
                ).update(state=SchemaState.PROVISIONING)
                _record_pending_source(workspace, tenant)
                # Queued first: both take the workspace lock W, and the load holds
                # it for its whole run, so on a worker with more than one slot a
                # rebuild dequeued second would wait out the load (or the lock
                # timeout) before coverage names the missing source.
                rebuild_workspace_view_schema.defer(workspace_id=str(workspace.id))
                intent = capture_load_intent([tenant.id], INTENT_RECONCILE_MISSING)
                materialize_workspace.defer(
                    workspace_id=str(workspace.id),
                    user_id=str(actor_id),
                    load_intent=intent,
                    only_unserved=True,
                    notify_thread=False,
                )

    return wt, created


def _record_pending_source(workspace, tenant) -> None:
    """Name ``tenant`` as missing from the serving views until a rebuild includes it.

    The queued rebuild may wait behind a long load (the worker runs one job at a
    time), and until then the ACTIVE row's coverage would otherwise still claim
    every source, so answers would omit this one without a warning.
    """
    entry = coverage_entry(tenant)
    # Locks rows in every state: a rebuild publishing a non-ACTIVE row as ACTIVE
    # holds this lock while it reads the workspace's sources, so one side always
    # sees the other (SchemaManager._name_sources_added_since).
    for vs in WorkspaceViewSchema.objects.select_for_update().filter(workspace=workspace):
        if vs.state != SchemaState.ACTIVE:
            continue
        if vs.tenant_coverage in (None, {}):
            coverage = _legacy_coverage(workspace, excluding=tenant)
        else:
            coverage = parse_coverage(vs.tenant_coverage)
            if coverage is None:
                # Already reported as unknown coverage; don't overwrite it.
                continue
        vs.tenant_coverage = {
            **coverage,
            "included_tenants": [
                e for e in coverage["included_tenants"] if e["tenant_id"] != entry["tenant_id"]
            ],
            "excluded_tenants": [
                *(e for e in coverage["excluded_tenants"] if e["tenant_id"] != entry["tenant_id"]),
                entry,
            ],
        }
        vs.save(update_fields=["tenant_coverage"])


def _legacy_coverage(workspace, *, excluding) -> dict:
    """Coverage for a row that never recorded any, by the rebuild's own rule.

    Only a source with an ACTIVE schema can be in the views, so a linked source
    without one is named missing rather than claimed as covered.
    """
    others = sorted(
        workspace.tenants.exclude(id=excluding.id),
        key=lambda t: (t.provider, t.external_id, str(t.id)),
    )
    serving = set(
        TenantSchema.objects.filter(tenant__in=others, state=SchemaState.ACTIVE).values_list(
            "tenant_id", flat=True
        )
    )
    return {
        "included_tenants": [coverage_entry(t) for t in others if t.id in serving],
        "excluded_tenants": [coverage_entry(t) for t in others if t.id not in serving],
    }


def remove_workspace_tenant(workspace, wt: WorkspaceTenant) -> None:
    """Remove a tenant from a workspace and reconcile the view schema.

    Deletes the WorkspaceTenant record. If the workspace remains multi-tenant
    (>=2 tenants left), marks any existing WorkspaceViewSchema as PROVISIONING
    and dispatches a rebuild. If the workspace drops to single-tenant (or zero),
    routing moves to the tenant schema and any active or provisioning view schema
    becomes an orphan — mark it TEARDOWN and dispatch teardown so the physical
    ``ws_<hash>`` schema is dropped.

    Both ``defer`` calls are transaction-safe — the procrastinate row is only
    visible to workers after commit.

    Raises ValidationError if wt is the last tenant in the workspace.
    """
    with transaction.atomic():
        # Lock tenant rows before counting so concurrent removals can't both pass
        # the last-tenant guard. Evaluate to a list because PostgreSQL forbids
        # FOR UPDATE with aggregates (.count()).
        tenant_ids = list(
            workspace.workspace_tenants.select_for_update().values_list("id", flat=True)
        )
        if len(tenant_ids) <= 1:
            raise ValidationError("Cannot remove the last tenant from a workspace.")
        wt.delete()
        _invalidate_on_commit(workspace)
        remaining = len(tenant_ids) - 1
        if remaining <= 1:
            # A PROVISIONING row has a rebuild queued or running; retiring it makes
            # that rebuild skip, or drop what it built, instead of publishing ACTIVE.
            for vs in WorkspaceViewSchema.objects.filter(
                workspace=workspace, state__in=[SchemaState.ACTIVE, SchemaState.PROVISIONING]
            ):
                vs.state = SchemaState.TEARDOWN
                vs.save(update_fields=["state"])
                teardown_view_schema_task.defer(view_schema_id=str(vs.id))
        else:
            WorkspaceViewSchema.objects.filter(workspace=workspace).update(
                state=SchemaState.PROVISIONING
            )
            rebuild_workspace_view_schema.defer(workspace_id=str(workspace.id))


async def touch_workspace_schemas(workspace) -> None:
    """Reset the inactivity TTL for a workspace's active schemas.

    Multi-tenant: touches the WorkspaceViewSchema *and* every constituent
    TenantSchema — chat activity never touches the underlying schemas directly,
    so without this they expire and their DROP CASCADE destroys the views inside
    the still-ACTIVE view schema.
    """
    tenant_count = await workspace.workspace_tenants.acount()
    if tenant_count == 1:
        tenant = await workspace.tenants.afirst()
        ts = await TenantSchema.objects.filter(
            tenant=tenant,
            state__in=[SchemaState.ACTIVE, SchemaState.MATERIALIZING],
        ).afirst()
        if ts is not None:
            await ts.atouch()
    elif tenant_count > 1:
        # Touch tenant schemas even if no view schema row exists — they underpin it.
        tenant_ids = [t.id async for t in workspace.tenants.all()]
        await TenantSchema.objects.filter(
            tenant_id__in=tenant_ids,
            state__in=[SchemaState.ACTIVE, SchemaState.MATERIALIZING],
        ).aupdate(last_accessed_at=timezone.now())

        vs = await WorkspaceViewSchema.objects.filter(
            workspace=workspace,
            state__in=[SchemaState.ACTIVE, SchemaState.MATERIALIZING],
        ).afirst()
        if vs is not None:
            await vs.atouch()
