"""List stored semantic SQL that would fail the Cube publication validators."""

import json
import uuid
from functools import partial

from django.core.management.base import BaseCommand
from django.db.models import Prefetch
from sqlglot.errors import TokenError

from apps.semantic.models import SemanticDataset, SemanticField, SemanticRelationship
from apps.semantic.services.custom_datasets import CustomDatasetError, custom_dataset_dependencies
from apps.semantic.services.field_sql import (
    SemanticSQLValidationError,
    compile_dimension_sql,
    compile_join_sql,
    compile_measure_filter_sql,
    compile_measure_sql,
    dataset_column_names,
)


class Command(BaseCommand):
    help = (
        "Read-only audit of stored semantic-model SQL (measure SQL and filters, calculated "
        "dimensions, join conditions, custom dataset SQL) against the validators applied "
        "when the Cube schema is generated. Nothing is modified."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--json",
            action="store_true",
            help="Emit a JSON array instead of readable text.",
        )
        parser.add_argument(
            "--workspace-id",
            action="append",
            type=uuid.UUID,
            dest="workspace_ids",
            help="Limit the audit to a workspace UUID. May be repeated.",
        )

    def handle(self, *args, **options):
        findings = audit_semantic_sql(workspace_ids=options["workspace_ids"])
        if options["json"]:
            self.stdout.write(json.dumps(findings, indent=2, sort_keys=True))
            return
        for finding in findings:
            visibility = "" if finding["visible"] else " (hidden)"
            self.stdout.write(
                f"[{finding['kind']}] workspace_id={finding['workspace_id']} "
                f"object={finding['object']}{visibility} path={finding['path']}\n"
                f"  {finding['error']}"
            )
        self.stdout.write(f"Summary: {len(findings)} stored SQL fragment(s) fail validation.")


def audit_semantic_sql(*, workspace_ids=None) -> list[dict]:
    findings: list[dict] = []
    datasets = SemanticDataset.objects.prefetch_related(
        Prefetch("fields", queryset=SemanticField.objects.order_by("name"))
    ).order_by("workspace_id", "name")
    relationships = SemanticRelationship.objects.select_related(
        "from_dataset", "to_dataset"
    ).order_by("workspace_id", "name")
    if workspace_ids:
        datasets = datasets.filter(workspace_id__in=workspace_ids)
        relationships = relationships.filter(workspace_id__in=workspace_ids)

    def check(kind, owner, name, path, visible, validate):
        try:
            validate()
        # A malformed stored fragment must be reported, not abort the whole audit.
        except (SemanticSQLValidationError, CustomDatasetError, TokenError, ValueError) as exc:
            findings.append(
                {
                    "kind": kind,
                    "workspace_id": str(owner["workspace_id"]),
                    "object": name,
                    "object_id": str(owner["id"]),
                    "path": path,
                    "visible": visible,
                    "error": str(exc),
                }
            )

    for dataset in datasets:
        metadata = dataset.metadata or {}
        dataset_owner = {"workspace_id": dataset.workspace_id, "id": dataset.id}
        if dataset.source_kind == SemanticDataset.SourceKind.CUSTOM:
            for key in ("cube_sql", "sql"):
                if source := metadata.get(key):
                    check(
                        "custom_dataset",
                        dataset_owner,
                        dataset.name,
                        f"metadata.{key}",
                        dataset.is_visible,
                        partial(custom_dataset_dependencies, source),
                    )
        columns = dataset_column_names(dataset)
        for field in dataset.fields.all():
            field_metadata = field.metadata or {}
            owner = {"workspace_id": dataset.workspace_id, "id": field.id}
            name = f"{dataset.name}.{field.name}"
            visible = dataset.is_visible and field.is_visible
            cube_sql = field_metadata.get("cube_sql")
            if field.field_type != SemanticField.FieldType.MEASURE:
                if cube_sql:
                    check(
                        "dimension",
                        owner,
                        name,
                        "metadata.cube_sql",
                        visible,
                        partial(compile_dimension_sql, cube_sql, columns=columns),
                    )
                continue
            if isinstance(cube_sql, str) and cube_sql.strip():
                check(
                    "measure",
                    owner,
                    name,
                    "metadata.cube_sql",
                    visible,
                    partial(compile_measure_sql, cube_sql, columns=columns),
                )
            filters = field_metadata.get("filters")
            for index, item in enumerate(filters if isinstance(filters, list) else []):
                sql = item.get("sql") if isinstance(item, dict) else None
                if isinstance(sql, str) and sql.strip():
                    check(
                        "measure_filter",
                        owner,
                        name,
                        f"metadata.filters[{index}].sql",
                        visible,
                        partial(compile_measure_filter_sql, sql, columns=columns),
                    )

    for relationship in relationships:
        check(
            "relationship",
            {"workspace_id": relationship.workspace_id, "id": relationship.id},
            relationship.name,
            "join_expression",
            relationship.from_dataset.is_visible and relationship.to_dataset.is_visible,
            partial(compile_join_sql, relationship.join_expression),
        )
    return findings
