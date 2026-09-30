"""List stored semantic SQL that would fail the Cube publication validators."""

import json
import uuid
from functools import partial

from django.core.management.base import BaseCommand
from django.db.models import Prefetch
from sqlglot.errors import SqlglotError

from apps.semantic.models import (
    SemanticDataset,
    SemanticField,
    SemanticModel,
    SemanticRelationship,
)
from apps.semantic.services.cube import (
    cube_member_references,
    publishable_datasets,
    published_member_references,
)
from apps.semantic.services.cube_sql import CubeSQLReferenceError, embed_cube_sql
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
    models = SemanticModel.objects.order_by("workspace_id")
    if workspace_ids:
        models = models.filter(workspace_id__in=workspace_ids)
    findings: list[dict] = []
    for model in models:
        findings.extend(_audit_model(model))
    return findings


def _audit_model(model: SemanticModel) -> list[dict]:
    findings: list[dict] = []
    datasets = list(
        model.datasets.prefetch_related(
            Prefetch("fields", queryset=SemanticField.objects.order_by("name"))
        ).order_by("name")
    )
    published = {dataset.id for dataset in publishable_datasets(datasets)}
    references = published_member_references([d for d in datasets if d.id in published])

    def check(kind, object_id, name, path, visible, validate):
        # A malformed stored fragment must be reported, not abort the whole audit.
        try:
            validate()
        except (
            SemanticSQLValidationError,
            CustomDatasetError,
            CubeSQLReferenceError,
            SqlglotError,
        ) as exc:
            findings.append(
                {
                    "kind": kind,
                    "workspace_id": str(model.workspace_id),
                    "object": name,
                    "object_id": str(object_id),
                    "path": path,
                    "visible": visible,
                    "error": str(exc),
                }
            )

    def published_sql(compile_sql, member_references, *, visible):
        # Unpublished members never reach embedding, so only their SQL itself is checked.
        def validate():
            sql = compile_sql()
            if visible:
                embed_cube_sql(sql, references=member_references)

        return validate

    for dataset in datasets:
        metadata = dataset.metadata or {}
        if dataset.source_kind == SemanticDataset.SourceKind.CUSTOM:
            key = "cube_sql" if metadata.get("cube_sql") else "sql"
            if source := metadata.get(key):
                check(
                    "custom_dataset",
                    dataset.id,
                    dataset.name,
                    f"metadata.{key}",
                    dataset.is_visible,
                    partial(custom_dataset_dependencies, source),
                )
        columns = dataset_column_names(dataset)
        member_references = cube_member_references(references, dataset)
        for field in dataset.fields.all():
            field_metadata = field.metadata or {}
            name = f"{dataset.name}.{field.name}"
            visible = dataset.id in published and field.is_visible
            cube_sql = field_metadata.get("cube_sql")
            if field.field_type != SemanticField.FieldType.MEASURE:
                if cube_sql:
                    check(
                        "dimension",
                        field.id,
                        name,
                        "metadata.cube_sql",
                        visible,
                        partial(compile_dimension_sql, cube_sql, columns=columns),
                    )
                continue
            if isinstance(cube_sql, str) and cube_sql.strip():
                check(
                    "measure",
                    field.id,
                    name,
                    "metadata.cube_sql",
                    visible,
                    published_sql(
                        partial(compile_measure_sql, cube_sql, columns=columns),
                        member_references,
                        visible=visible,
                    ),
                )
            filters = field_metadata.get("filters")
            for index, item in enumerate(filters if isinstance(filters, list) else []):
                sql = item.get("sql") if isinstance(item, dict) else None
                if isinstance(sql, str) and sql.strip():
                    check(
                        "measure_filter",
                        field.id,
                        name,
                        f"metadata.filters[{index}].sql",
                        visible,
                        published_sql(
                            partial(compile_measure_filter_sql, sql, columns=columns),
                            member_references,
                            visible=visible,
                        ),
                    )

    datasets_by_id = {dataset.id: dataset for dataset in datasets}
    relationships = SemanticRelationship.objects.filter(workspace_id=model.workspace_id)
    for relationship in relationships.order_by("name"):
        from_dataset = datasets_by_id.get(relationship.from_dataset_id)
        # Generation drops a join without a primary key before reading its SQL.
        visible = (
            {relationship.from_dataset_id, relationship.to_dataset_id} <= published
            and from_dataset is not None
            and bool(from_dataset.primary_key)
        )
        columns = dataset_column_names(from_dataset) if from_dataset is not None else set()
        check(
            "relationship",
            relationship.id,
            relationship.name,
            "join_expression",
            visible,
            published_sql(
                partial(compile_join_sql, relationship.join_expression, columns=columns),
                references | {"CUBE"},
                visible=visible,
            ),
        )
    return findings
