"""Read-only audit of knowledge and learnings against the workspace's current catalog.

Detection only (issue #264). Whether drifted rows are hidden, flagged or retired is a
pending product decision, so nothing here writes or changes what the retriever serves.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from uuid import UUID

from django.db.models import Exists, OuterRef

from apps.knowledge.models import AgentLearning, KnowledgeEntry, TableKnowledge
from apps.semantic.models import SemanticDataset
from apps.semantic.services.catalog import SemanticCatalogUnavailable, get_active_semantic_model
from apps.workspaces.models import Workspace

_BACKTICKED_RE = re.compile(r"`([^`\s]+)`")


@dataclass(frozen=True)
class DriftedReference:
    kind: str
    name: str
    reason: str


@dataclass
class DriftedRow:
    source: str
    id: str
    label: str
    references: list[DriftedReference]


@dataclass
class WorkspaceDriftReport:
    workspace_id: str
    workspace_name: str
    catalog_status: str
    catalog_version: int | None = None
    checked: int = 0
    drifted: list[DriftedRow] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


class _Catalog:
    """Names the active semantic model exposes now, and names it once had but hides."""

    def __init__(self, model) -> None:
        self.live_tables: set[str] = set()
        self.known_tables: set[str] = set()
        self.live_columns: dict[str, set[str]] = defaultdict(set)
        self.known_columns: dict[str, set[str]] = defaultdict(set)
        self.live_members: set[str] = set()
        self.known_members: set[str] = set()
        for dataset in SemanticDataset.objects.filter(semantic_model=model).prefetch_related(
            "fields"
        ):
            metadata = dataset.metadata or {}
            names = {dataset.table_name, dataset.name, metadata.get("source_table_name")} - {
                None,
                "",
            }
            self.known_tables |= names
            if dataset.is_visible:
                self.live_tables |= names
            for semantic_field in dataset.fields.all():
                member = f"{dataset.name}.{semantic_field.name}"
                column = (semantic_field.metadata or {}).get("source_column")
                live = dataset.is_visible and semantic_field.is_visible
                self.known_members.add(member)
                if live:
                    self.live_members.add(member)
                if not column:
                    continue
                for name in names:
                    self.known_columns[name].add(column)
                    if live:
                        self.live_columns[name].add(column)

    def table(self, name: str) -> DriftedReference | None:
        if name in self.live_tables:
            return None
        return DriftedReference("table", name, _reason(name in self.known_tables))

    def column(self, table: str, column: str) -> DriftedReference | None:
        if column in self.live_columns.get(table, ()):
            return None
        reason = _reason(column in self.known_columns.get(table, ()))
        return DriftedReference("column", f"{table}.{column}", reason)

    def member(self, name: str) -> DriftedReference | None:
        if name in self.live_members:
            return None
        return DriftedReference("member", name, _reason(name in self.known_members))

    def text_references(self, *texts: str) -> list[DriftedReference | None]:
        """Backticked names the catalog knows of but no longer serves.

        Free text is only matched against exact names this catalog has held, so a
        code span that was never a member or table (`docs.md`) is not reported. A
        member whose field row was deleted outright is therefore not detected either.
        """
        references = []
        for text in texts:
            for token in _BACKTICKED_RE.findall(text):
                if token in self.known_members:
                    references.append(self.member(token))
                elif token in self.known_tables:
                    references.append(self.table(token))
        return references


def _reason(known: bool) -> str:
    return "hidden" if known else "missing"


def _unique(references: list[DriftedReference | None]) -> list[DriftedReference]:
    return [reference for reference in dict.fromkeys(references) if reference is not None]


def _names(values) -> list[str]:
    return [value for value in values or [] if isinstance(value, str) and value]


def _table_knowledge_references(row: TableKnowledge, catalog: _Catalog) -> list[DriftedReference]:
    column_notes = row.column_notes or {}
    table_reference = catalog.table(row.table_name)
    references = [table_reference]
    # A hidden or missing table already explains every column note on it.
    if table_reference is None:
        references += [catalog.column(row.table_name, c) for c in sorted(column_notes)]
    relations = [r for r in row.related_tables or [] if isinstance(r, dict | str)]
    related = [r.get("table") if isinstance(r, dict) else r for r in relations]
    references += [catalog.table(name) for name in _names(related)]
    # Prose KnowledgeRetriever renders that can name catalog objects; use_cases is not rendered.
    prose = [
        row.description,
        *_names(row.data_quality_notes),
        *_names(column_notes.values()),
        *_names(r.get("join_hint") for r in relations if isinstance(r, dict)),
    ]
    references += catalog.text_references(*prose)
    return _unique(references)


def _audit_workspace(workspace: Workspace) -> WorkspaceDriftReport:
    report = WorkspaceDriftReport(
        workspace_id=str(workspace.id),
        workspace_name=workspace.name,
        catalog_status="active",
    )
    try:
        model = get_active_semantic_model(workspace)
    except SemanticCatalogUnavailable:
        report.catalog_status = "unavailable"
        return report
    report.catalog_version = model.version
    catalog = _Catalog(model)

    rows: list[tuple[str, str, str, list[DriftedReference]]] = []
    for entry in KnowledgeEntry.objects.filter(workspace=workspace).order_by("title", "id"):
        references = _unique(catalog.text_references(entry.content))
        rows.append(("knowledge_entry", str(entry.id), entry.title, references))
    for table in TableKnowledge.objects.filter(workspace=workspace).order_by("table_name"):
        references = _table_knowledge_references(table, catalog)
        rows.append(("table_knowledge", str(table.id), table.table_name, references))
    # Inactive learnings never reach the prompt, so their drift is not actionable.
    for learning in AgentLearning.objects.filter(workspace=workspace, is_active=True).order_by(
        "created_at", "id"
    ):
        references = _unique(
            [catalog.table(name) for name in _names(learning.applies_to_tables)]
            + catalog.text_references(learning.description)
        )
        rows.append(("agent_learning", str(learning.id), learning.description[:80], references))

    report.checked = len(rows)
    report.drifted = [
        DriftedRow(source, row_id, label, references)
        for source, row_id, label, references in rows
        if references
    ]
    return report


def audit_knowledge_drift(
    *, workspace_ids: Iterable[UUID | str] | None = None
) -> list[WorkspaceDriftReport]:
    """Report, per workspace holding knowledge, rows whose references the catalog no longer serves.

    Explicitly requested workspaces are always reported, even with nothing to check,
    so a requested id is never silently dropped.

    A workspace without an active semantic model is reported as ``unavailable`` and
    not audited: with no catalog to compare against, every reference would look drifted.
    Issues about six queries per audited workspace; this is an operator command, so
    that is preferred over batching every workspace's catalog into memory at once.
    """
    if workspace_ids is not None:
        workspaces = Workspace.objects.filter(id__in=list(workspace_ids))
    else:
        workspaces = Workspace.objects.filter(
            Exists(KnowledgeEntry.objects.filter(workspace=OuterRef("pk")))
            | Exists(TableKnowledge.objects.filter(workspace=OuterRef("pk")))
            | Exists(AgentLearning.objects.filter(workspace=OuterRef("pk"), is_active=True))
        )
    return [_audit_workspace(workspace) for workspace in workspaces.order_by("name", "id")]
