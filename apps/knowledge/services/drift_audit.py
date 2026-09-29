"""Read-only audit of knowledge and learnings against the workspace's current catalog.

Detection only (issue #264). Whether drifted rows are hidden, flagged or retired is a
pending product decision, so nothing here writes or changes what the retriever serves.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import asdict, dataclass, field

from django.db.models import Exists, OuterRef

from apps.knowledge.models import AgentLearning, KnowledgeEntry, TableKnowledge
from apps.semantic.models import SemanticDataset
from apps.semantic.services.catalog import SemanticCatalogUnavailable, get_active_semantic_model
from apps.workspaces.models import Workspace

_BACKTICKED_RE = re.compile(r"`([^`\s]+)`")
_MEMBER_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)$")


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
        self.known_datasets: set[str] = set()
        for dataset in SemanticDataset.objects.filter(semantic_model=model).prefetch_related(
            "fields"
        ):
            metadata = dataset.metadata or {}
            names = {dataset.table_name, dataset.name, metadata.get("source_table_name")} - {
                None,
                "",
            }
            self.known_tables |= names
            self.known_datasets.add(dataset.name)
            if dataset.is_visible:
                self.live_tables |= names
            for semantic_field in dataset.fields.all():
                member = f"{dataset.name}.{semantic_field.name}"
                column = (semantic_field.metadata or {}).get("source_column")
                live = dataset.is_visible and semantic_field.is_visible
                self.known_members.add(member)
                if live:
                    self.live_members.add(member)
                for name in names if column else ():
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

    def text_references(self, text: str) -> list[DriftedReference]:
        """Backticked names the catalog knows of but no longer serves.

        Free text is only matched against names this catalog has held, so a code
        span that was never a dataset member or table is not reported.
        """
        drifted: dict[str, DriftedReference] = {}
        for token in _BACKTICKED_RE.findall(text):
            member = _MEMBER_RE.match(token)
            if member and member.group(1) in self.known_datasets:
                reference = self.member(token)
            elif token in self.known_tables:
                reference = self.table(token)
            else:
                reference = None
            if reference is not None:
                drifted[token] = reference
        return list(drifted.values())


def _reason(known: bool) -> str:
    return "hidden" if known else "missing"


def _table_knowledge_references(row: TableKnowledge, catalog: _Catalog) -> list[DriftedReference]:
    missing_table = catalog.table(row.table_name)
    if missing_table is not None:
        references = [missing_table]
    else:
        references = [
            reference
            for column in sorted(row.column_notes or {})
            if (reference := catalog.column(row.table_name, column)) is not None
        ]
    for relation in row.related_tables or []:
        related = relation.get("table") if isinstance(relation, dict) else relation
        if isinstance(related, str) and related:
            reference = catalog.table(related)
            if reference is not None:
                references.append(reference)
    return references


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
    for entry in KnowledgeEntry.objects.filter(workspace=workspace).order_by("title"):
        rows.append(
            ("knowledge_entry", str(entry.id), entry.title, catalog.text_references(entry.content))
        )
    for table in TableKnowledge.objects.filter(workspace=workspace).order_by("table_name"):
        rows.append(
            (
                "table_knowledge",
                str(table.id),
                table.table_name,
                _table_knowledge_references(table, catalog),
            )
        )
    # Inactive learnings never reach the prompt, so their drift is not actionable.
    for learning in AgentLearning.objects.filter(workspace=workspace, is_active=True).order_by(
        "created_at"
    ):
        references = [
            reference
            for name in learning.applies_to_tables or []
            if isinstance(name, str) and (reference := catalog.table(name)) is not None
        ]
        references += catalog.text_references(learning.description)
        rows.append(("agent_learning", str(learning.id), learning.description[:80], references))

    report.checked = len(rows)
    report.drifted = [
        DriftedRow(source, row_id, label, references)
        for source, row_id, label, references in rows
        if references
    ]
    return report


def audit_knowledge_drift(workspace_ids=None) -> list[WorkspaceDriftReport]:
    """Report, per workspace holding knowledge, rows whose references the catalog no longer serves.

    A workspace without an active semantic model is reported as ``unavailable`` and
    not audited: with no catalog to compare against, every reference would look drifted.
    """
    workspaces = Workspace.objects.filter(
        Exists(KnowledgeEntry.objects.filter(workspace=OuterRef("pk")))
        | Exists(TableKnowledge.objects.filter(workspace=OuterRef("pk")))
        | Exists(AgentLearning.objects.filter(workspace=OuterRef("pk"), is_active=True))
    ).order_by("name", "id")
    if workspace_ids:
        workspaces = workspaces.filter(id__in=workspace_ids)
    return [_audit_workspace(workspace) for workspace in workspaces]
