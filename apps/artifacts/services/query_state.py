"""Read-only, artifact-specific checks on the workspace query surface."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

import yaml
from django.utils.dateparse import parse_datetime

from apps.semantic.models import (
    CubeSchema,
    CustomDataset,
    SemanticDataset,
    SemanticField,
    SemanticModel,
)
from apps.semantic.services.custom_datasets import (
    CustomDatasetError,
    custom_dataset_dependencies,
)
from apps.workspaces.models import (
    SchemaState,
    TenantSchema,
    WorkspaceDataRecovery,
    WorkspaceViewSchema,
)
from apps.workspaces.services.query_state import (
    included_tenant_snapshot_state,
    workspace_query_surface,
)
from apps.workspaces.services.schema_manager import SchemaManager
from apps.workspaces.services.tenant_coverage import parse_coverage
from apps.workspaces.services.view_sources import ViewSourcesError, parse_view_sources


def _members(queries):
    for query in queries:
        for key, allowed in (
            ("measures", {SemanticField.FieldType.MEASURE}),
            (
                "dimensions",
                {SemanticField.FieldType.DIMENSION, SemanticField.FieldType.TIME_DIMENSION},
            ),
        ):
            values = query.get(key) or []
            for member in values if isinstance(values, list) else [values]:
                yield member, allowed
        time = query.get("time_dimension") or query.get("timeDimension")
        if time:
            yield time, {SemanticField.FieldType.TIME_DIMENSION}
        for item in query.get("filters") or []:
            if isinstance(item, dict):
                yield (
                    item.get("field") or item.get("member"),
                    {
                        SemanticField.FieldType.DIMENSION,
                        SemanticField.FieldType.TIME_DIMENSION,
                    },
                )


def _model_repair(surface, detail):
    return {
        **surface,
        "status": "model_drift",
        "queryable": False,
        "recovery_action": None,
        "message": "This artifact references a dataset or field that is no longer available.",
        "detail": f"{detail} Review its data model or update the artifact in chat. Reloading data may not fix this.",
    }


def _source_repair(surface, dataset, *, view_schema):
    """Re-establish attribution without guessing a tenant or reloading providers."""
    if surface["status"] == "recovering":
        return surface
    action = "view_rebuild" if view_schema else "semantic_rebuild"
    return {
        **surface,
        "status": f"needs_{action}",
        "queryable": False,
        "recovery_action": action,
        "message": "This artifact's data source cannot be identified safely.",
        "detail": (
            f"Source attribution for dataset '{dataset.name}' is missing or inconsistent. "
            "Rebuild the workspace query layer to verify its source. "
            "This repair does not reload provider data."
        ),
    }


def _view_excludes_owner(view, owners: set[str], tenant_ids: set[str]) -> bool:
    """Accept an omitted source only with a scoped, unambiguous exclusion."""
    coverage = parse_coverage(view.tenant_coverage)
    if coverage is None or len(owners) != 1 or not owners <= tenant_ids:
        return False
    included = {entry["tenant_id"] for entry in coverage["included_tenants"]}
    excluded = {entry["tenant_id"] for entry in coverage["excluded_tenants"]}
    return (
        included | excluded == tenant_ids
        and not included & excluded
        and len(included) == len(coverage["included_tenants"])
        and len(excluded) == len(coverage["excluded_tenants"])
        and owners <= excluded
    )


def _promoted_members(content):
    try:
        document = yaml.safe_load(content)
    except yaml.YAMLError:
        return {}
    if not isinstance(document, dict) or not isinstance(document.get("cubes"), list):
        return {}
    members = {}
    for cube in document["cubes"]:
        if not isinstance(cube, dict):
            continue
        for section in ("measures", "dimensions"):
            for field in cube.get(section) or []:
                if isinstance(field, dict):
                    members[f"{cube.get('name')}.{field.get('name')}"] = (
                        SemanticField.FieldType.MEASURE
                        if section == "measures"
                        else SemanticField.FieldType.TIME_DIMENSION
                        if field.get("type") == "time"
                        else SemanticField.FieldType.DIMENSION
                    )
    return members


async def artifact_query_surface(artifact) -> dict[str, Any]:
    """Check required sources, not every source attempted by a workspace refresh.

    Catalog reads are deliberately not physical probes or catalog refreshes.
    A known absent source is recoverable; a renamed/hidden member is a model
    edit, not permission to reload provider data. Legacy catalogs may use known
    tenant schemas or unambiguous names, but unknown ownership is not readiness.
    """
    surface = await workspace_query_surface(artifact.workspace)
    model = await SemanticModel.objects.filter(workspace=artifact.workspace).afirst()
    if model is None:
        return surface
    if model.status == SemanticModel.Status.DRAFT:
        return _model_repair(surface, "The workspace data model is a draft.")

    datasets = {
        dataset.name: dataset
        async for dataset in SemanticDataset.objects.filter(semantic_model=model)
        .select_related("custom_dataset")
        .prefetch_related("fields")
    }
    required = {}
    member_error = ""
    members = list(_members(artifact.semantic_queries))
    for member, allowed in members:
        if not isinstance(member, str) or "." not in member:
            member_error = "A semantic member is invalid."
            break
        name, field_name = member.split(".", 1)
        dataset = datasets.get(name)
        if dataset is None:
            member_error = f"Dataset '{name}' is missing."
            break
        required[name] = dataset
        field = next((field for field in dataset.fields.all() if field.name == field_name), None)
        if field is None or not field.is_visible or field.field_type not in allowed:
            member_error = f"Field '{member}' is missing, hidden, or has changed type."
            break

    # A first/failed bootstrap may have no catalog yet. An existing catalog,
    # even without an ACTIVE Cube, can still prove a deliberate member edit.
    if member_error and not datasets and surface["semantic_status"] in {"unknown", "unavailable"}:
        return surface
    if member_error:
        return _model_repair(surface, member_error)

    physical = {}
    aliases = defaultdict(list)
    for dataset in datasets.values():
        if dataset.source_kind == SemanticDataset.SourceKind.PHYSICAL:
            for alias in {dataset.name.lower(), dataset.table_name.lower()}:
                aliases[alias].append(dataset)
    hidden = []
    for dataset in required.values():
        if (
            "is_visible" in (dataset.metadata or {}).get("curated_fields", [])
            and not dataset.is_visible
        ):
            return _model_repair(surface, f"Dataset '{dataset.name}' was hidden in the data model.")
        if dataset.source_kind == SemanticDataset.SourceKind.PHYSICAL:
            physical[dataset.name] = dataset
        else:
            custom = dataset.custom_dataset
            if (
                custom is None
                or not custom.is_visible
                or custom.status == CustomDataset.Status.DRAFT
            ):
                return _model_repair(surface, f"Custom dataset '{dataset.name}' is not active.")
            try:
                dependencies = custom_dataset_dependencies(custom.definition_sql)
            except CustomDatasetError:
                return _model_repair(surface, f"Custom dataset '{dataset.name}' needs SQL repair.")
            for name in dependencies:
                matches = aliases.get(name, [])
                if len(matches) != 1:
                    return _model_repair(
                        surface,
                        f"The source of custom dataset '{dataset.name}' is missing or ambiguous.",
                    )
                physical[matches[0].name] = matches[0]
        if not dataset.is_visible:
            hidden.append(dataset)

    tenants = [tenant async for tenant in artifact.workspace.tenants.all()]
    tenant_ids = {str(tenant.id) for tenant in tenants}
    schemas = [schema async for schema in TenantSchema.objects.filter(tenant_id__in=tenant_ids)]
    active_ids = {str(schema.tenant_id) for schema in schemas if schema.state == SchemaState.ACTIVE}
    schema_owners = {schema.schema_name: str(schema.tenant_id) for schema in schemas}
    view_schemas = {
        view.schema_name: view
        async for view in WorkspaceViewSchema.objects.filter(workspace=artifact.workspace)
    }
    coverage = parse_coverage(surface.get("tenant_coverage"))
    included = {entry["tenant_id"] for entry in coverage["included_tenants"]} if coverage else None
    required_ids = set()
    unpublished_ids = set()
    for dataset in physical.values():
        view = view_schemas.get(dataset.schema_name)
        try:
            sources = (
                parse_view_sources(view.view_sources, tenant_ids) if view is not None else None
            )
        except ViewSourcesError:
            return _source_repair(surface, dataset, view_schema=True)
        published_source = sources.get(dataset.table_name) if sources is not None else None
        metadata = dataset.metadata or {}
        provenance = metadata.get("source_tenant_ids")
        if "source_tenant_ids" in metadata:
            if not (
                isinstance(provenance, list)
                and provenance
                and all(isinstance(item, str) for item in provenance)
            ):
                return _source_repair(surface, dataset, view_schema=view is not None)
            owners = set(provenance)
            if published_source is not None and owners != {published_source.tenant_id}:
                return _source_repair(surface, dataset, view_schema=True)
            # Last-good provenance survives a partial/failed rebuild. Attempted
            # coverage can explain an omission even when views are unavailable;
            # it cannot prove ownership or readiness. Current ACTIVE schemas below
            # still decide whether a known excluded source needs loading.
            if sources is not None and published_source is None:
                if not _view_excludes_owner(view, owners, tenant_ids):
                    return _source_repair(surface, dataset, view_schema=True)
                # A newer explicit omission cannot be erased by the workspace
                # surface's earlier coverage snapshot, even if the source reloads.
                unpublished_ids.update(owners)
        elif published_source is not None:
            owners = {published_source.tenant_id}
        elif dataset.schema_name in schema_owners:
            owners = {schema_owners[dataset.schema_name]}
        elif view is not None and sources is None:
            owners = set(SchemaManager().tenant_ids_for_view(dataset.table_name, tenants))
        else:
            owners = set()
        if owners - tenant_ids:
            return _model_repair(
                surface, f"A source for dataset '{dataset.name}' is no longer in this workspace."
            )
        if not owners:
            return _source_repair(surface, dataset, view_schema=view is not None)
        required_ids.update(owners)
        if not dataset.is_visible:
            hidden.append(dataset)

    missing_sources = required_ids - active_ids
    missing_views = unpublished_ids | (required_ids - included if included is not None else set())
    if missing_sources or missing_views:
        action = "materialization" if missing_sources else "view_rebuild"
        if surface["status"] == "recovering":
            return surface
        return {
            **surface,
            "status": f"needs_{action}",
            "queryable": False,
            "recovery_action": action,
            "message": (
                "A data source required by this artifact is unavailable. Restore it to use the artifact again."
                if missing_sources
                else "A data source required by this artifact is missing from the workspace query layer. Rebuild that layer."
            ),
        }
    if surface["queryable"]:
        cube = await CubeSchema.objects.filter(
            workspace=artifact.workspace, semantic_model=model, status=CubeSchema.Status.ACTIVE
        ).afirst()
        if cube is not None:
            surface = {**surface, "data_revision": cube.updated_at.isoformat()}
        promoted = _promoted_members(cube.content) if cube is not None else {}
        if any(promoted.get(member) not in allowed for member, allowed in members):
            snapshot = await included_tenant_snapshot_state(
                artifact.workspace, surface.get("tenant_coverage")
            )
            action = "materialization" if snapshot == "unsafe" else "semantic_rebuild"
            return {
                **surface,
                "status": "recovering" if snapshot == "in_progress" else f"needs_{action}",
                "queryable": False,
                "recovery_action": None if snapshot == "in_progress" else action,
                "message": "The serving data model does not yet include fields required by this artifact.",
            }

    if hidden and surface["status"] == "ready":
        return _model_repair(
            surface, f"Dataset '{hidden[0].name}' is not available in the current data model."
        )

    if surface["queryable"] and surface["semantic_status"] in {"stale", "deferred"}:
        snapshot = await included_tenant_snapshot_state(
            artifact.workspace, surface.get("tenant_coverage")
        )
        return {
            **surface,
            "status": "recovering" if snapshot == "in_progress" else "ready",
            "recovery_action": (
                None
                if snapshot == "in_progress"
                else "materialization"
                if snapshot == "unsafe"
                else "semantic_rebuild"
            ),
            "message": (
                "Showing the last available data while the data model is being refreshed."
                if snapshot == "in_progress"
                else "Showing the last available data. The latest data model rebuild did not complete."
            ),
        }
    if surface["queryable"] and artifact.id is not None:
        previous = (
            await WorkspaceDataRecovery.objects.filter(
                workspace=artifact.workspace,
                source_type="artifact",
                source_id=artifact.id,
                state__in=[
                    WorkspaceDataRecovery.State.COMPLETED,
                    WorkspaceDataRecovery.State.FAILED,
                ],
            )
            .order_by("-created_at")
            .afirst()
        )
        if previous is not None and previous.state == WorkspaceDataRecovery.State.FAILED:
            # A failed dispatch/provider attempt may never reach the Cube build
            # recorder. Only a later verified build, not catalog updated_at,
            # proves that this old failure has been repaired by another session.
            last_build = (model.metadata or {}).get("last_build") or {}
            verified_at = parse_datetime(str(last_build.get("at") or ""))
            repaired = (
                last_build.get("ok") is True
                and verified_at is not None
                and verified_at.tzinfo is not None
                and verified_at
                >= (previous.completed_at or previous.started_at or previous.created_at)
            )
            if not repaired:
                return {
                    **surface,
                    "recovery_action": previous.recovery_type,
                    "message": "Showing the last available data. The latest repair did not complete.",
                    "detail": previous.error[:500],
                }
    return surface
