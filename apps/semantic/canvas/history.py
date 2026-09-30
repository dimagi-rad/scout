"""Data model history: a revision per saved change, and undo.

Every canvas commit records a ``SemanticModelRevision`` holding full before and
after snapshots of each object it touched. Undo writes the ``before`` side back
in one transaction and records itself as a new revision, so an undo can be
undone too. Undo refuses (and writes nothing) when a touched object changed
after the revision, because writing old values over a later edit would silently
lose that edit.
"""

from __future__ import annotations

import logging
from functools import cached_property
from typing import Any

from django.contrib.auth import get_user_model
from django.db import IntegrityError, OperationalError, transaction
from django.db.models import Q

from apps.semantic.canvas.objects import (
    field_sql_text,
    normalize_member_references,
    references_field,
)
from apps.semantic.canvas.service import allowed_custom_dataset_tables
from apps.semantic.models import (
    CustomDataset,
    SemanticCanvasChange,
    SemanticDataset,
    SemanticField,
    SemanticModel,
    SemanticModelRevision,
    SemanticRelationship,
)
from apps.semantic.services.custom_datasets import CustomDatasetError, compile_custom_dataset_sql

logger = logging.getLogger(__name__)

DATASET = "dataset"
FIELD = "field"
RELATIONSHIP = "relationship"
CREATE = "create"
UPDATE = "update"
DELETE = "delete"
CANVAS_SOURCE = "canvas"
CURATED_KEY = "metadata.curated_fields"

DATASET_COLUMNS = (
    "name",
    "label",
    "description",
    "source_kind",
    "schema_name",
    "table_name",
    "primary_key",
    "row_count",
    "is_visible",
)
FIELD_COLUMNS = (
    "name",
    "label",
    "description",
    "field_type",
    "data_type",
    "expression",
    "measure_type",
    "is_visible",
)
RELATIONSHIP_COLUMNS = ("name", "relationship_type", "join_expression")
CUSTOM_DATASET_COLUMNS = (
    "name",
    "label",
    "description",
    "definition_sql",
    "definition_json",
    "status",
    "is_visible",
)
# Refreshes rewrite the rest of field metadata (nullable, source_column, ...);
# only these keys are authored, so only they decide whether a field "changed".
FIELD_AUTHORED_METADATA_KEYS = ("format", "currency", "filters", "cube_sql")
SUMMARY_MAX_ITEMS = 3
VERBS = {CREATE: "Created", UPDATE: "Edited", DELETE: "Deleted"}


def refused(code: str, message: str, conflicts: list[dict] | None = None) -> dict[str, Any]:
    """An undo that wrote nothing; a plain result, so no exception text reaches a response."""
    return {"refused": {"code": code, "message": message, "conflicts": conflicts or []}}


def snapshot_field(field: SemanticField) -> dict[str, Any]:
    return {
        "id": str(field.id),
        "dataset_id": str(field.dataset_id),
        "dataset_name": field.dataset.name,
        **{column: getattr(field, column) for column in FIELD_COLUMNS},
        "metadata": dict(field.metadata or {}),
    }


def snapshot_relationship(relationship: SemanticRelationship) -> dict[str, Any]:
    return {
        "id": str(relationship.id),
        "from_dataset_id": str(relationship.from_dataset_id),
        "to_dataset_id": str(relationship.to_dataset_id),
        **{column: getattr(relationship, column) for column in RELATIONSHIP_COLUMNS},
        "metadata": dict(relationship.metadata or {}),
    }


def snapshot_dataset(dataset: SemanticDataset, *, deep: bool) -> dict[str, Any]:
    """``deep`` adds the custom definition, fields and relationships, which a
    create or delete needs to be reversed; an edit needs only the row."""
    snapshot: dict[str, Any] = {
        "id": str(dataset.id),
        **{column: getattr(dataset, column) for column in DATASET_COLUMNS},
        "metadata": dict(dataset.metadata or {}),
    }
    if not deep:
        return snapshot
    custom = dataset.custom_dataset
    snapshot["custom_dataset"] = (
        None
        if custom is None
        else {
            "id": str(custom.id),
            "created_by_id": str(custom.created_by_id) if custom.created_by_id else None,
            **{column: getattr(custom, column) for column in CUSTOM_DATASET_COLUMNS},
        }
    )
    snapshot["fields"] = [
        snapshot_field(field) for field in dataset.fields.select_related("dataset").order_by("name")
    ]
    relationships = SemanticRelationship.objects.filter(
        Q(from_dataset=dataset) | Q(to_dataset=dataset), workspace_id=dataset.workspace_id
    ).order_by("name")
    snapshot["relationships"] = [snapshot_relationship(rel) for rel in relationships]
    return snapshot


def snapshot_object(object_type: str, obj, *, deep: bool = False) -> dict[str, Any]:
    if object_type == DATASET:
        return snapshot_dataset(obj, deep=deep)
    if object_type == FIELD:
        return snapshot_field(obj)
    return snapshot_relationship(obj)


def display_name(object_type: str, snapshot: dict[str, Any] | None) -> str:
    if not snapshot:
        return ""
    if object_type == FIELD:
        return f"{snapshot.get('dataset_name', '')}.{snapshot.get('name', '')}"
    return snapshot.get("name", "")


def change_entry(
    object_type: str,
    object_uuid,
    change_type: str,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "object_type": object_type,
        "object_uuid": str(object_uuid),
        "change_type": change_type,
        "name": display_name(object_type, after or before),
        "before": before,
        "after": after,
    }


def summarize(entries: list[dict[str, Any]]) -> str:
    parts = [
        f"{VERBS.get(entry['change_type'], 'Changed')} {entry['object_type']} {entry['name']}"
        for entry in entries[:SUMMARY_MAX_ITEMS]
    ]
    if len(entries) > SUMMARY_MAX_ITEMS:
        parts.append(f"and {len(entries) - SUMMARY_MAX_ITEMS} more")
    return "; ".join(parts)[:500]


def record_revision(
    workspace,
    entries: list[dict[str, Any]],
    *,
    source: str,
    user=None,
    thread_id=None,
    reverts: SemanticModelRevision | None = None,
    summary: str | None = None,
) -> SemanticModelRevision:
    return SemanticModelRevision.objects.create(
        workspace=workspace,
        source=source,
        summary=summary if summary is not None else summarize(entries),
        changes=entries,
        reverts=reverts,
        thread_id=thread_id,
        created_by=user if getattr(user, "is_authenticated", False) else None,
    )


def serialize_revision(revision: SemanticModelRevision, *, undone: bool) -> dict[str, Any]:
    creator = revision.created_by
    return {
        "id": str(revision.id),
        "source": revision.source,
        "summary": revision.summary,
        "created_at": revision.created_at.isoformat(),
        "created_by": (
            None
            if creator is None
            else {"id": str(creator.id), "name": creator.get_full_name() or creator.email}
        ),
        "thread_id": str(revision.thread_id) if revision.thread_id else None,
        "reverts_id": str(revision.reverts_id) if revision.reverts_id else None,
        "undone": undone,
        "changes": [
            {
                "object_type": entry.get("object_type"),
                "change_type": entry.get("change_type"),
                "name": entry.get("name"),
            }
            for entry in revision.changes or []
        ],
    }


def list_revisions(workspace, limit: int = 50) -> list[dict[str, Any]]:
    revisions = list(
        SemanticModelRevision.objects.filter(workspace=workspace)
        .select_related("created_by")
        .order_by("-created_at")[:limit]
    )
    undone_ids = set(
        SemanticModelRevision.objects.filter(reverts__in=revisions).values_list(
            "reverts_id", flat=True
        )
    )
    return [serialize_revision(rev, undone=rev.id in undone_ids) for rev in revisions]


def undo_revision(workspace, revision_id, user=None, thread_id=None) -> dict[str, Any]:
    """Reverse one revision atomically, or return ``refused(...)`` having written nothing."""
    with transaction.atomic():
        revision = (
            SemanticModelRevision.objects.select_for_update()
            .filter(id=revision_id, workspace=workspace)
            .first()
        )
        if revision is None:
            return refused("NOT_FOUND", "No such data model revision in this workspace.")
        if SemanticModelRevision.objects.filter(reverts=revision).exists():
            return refused("ALREADY_UNDONE", "This revision has already been undone.")
        model = _lock_model(workspace)
        if model is None:
            return refused(
                "CATALOG_BUSY", "The data model is being refreshed. Try the undo again shortly."
            )
        entries = list(revision.changes or [])
        restoring = {
            entry["object_uuid"]
            for entry in entries
            if entry["object_type"] == DATASET and entry.get("after") is None
        }
        removing = {entry["object_uuid"] for entry in entries if entry.get("before") is None}
        refs = _References(model)
        conflicts = [
            conflict
            for entry in entries
            if (conflict := _undo_conflict(workspace, model, entry, restoring, removing, refs))
            is not None
        ]
        if conflicts:
            return refused(
                "CONFLICT",
                "Undoing this revision would overwrite or break a later change. Undo the later "
                "revision first, or edit the objects directly.",
                conflicts,
            )
        undo_entries = _apply_undo(workspace, model, entries)
        if undo_entries is None:
            return refused("CONFLICT", "The restored objects collide with the current data model.")
        removed = [entry["object_uuid"] for entry in undo_entries if entry["after"] is None]
        # A settled canvas row over a now-missing object would read as a conflict.
        SemanticCanvasChange.objects.filter(
            canvas__workspace=workspace,
            object_uuid__in=removed,
            change_type=SemanticCanvasChange.ChangeType.UPDATE,
            fields={},
        ).delete()
        undo = record_revision(
            workspace,
            undo_entries,
            source=SemanticModelRevision.Source.UNDO,
            user=user,
            thread_id=thread_id,
            reverts=revision,
            summary=f"Undid: {revision.summary}"[:500],
        )
    return {
        "undone": serialize_revision(revision, undone=True),
        "revision": serialize_revision(undo, undone=False),
    }


def _apply_undo(workspace, model, entries) -> list[dict[str, Any]] | None:
    """None when a write hit a uniqueness race the checks could not see; rolled back."""
    try:
        with transaction.atomic():
            return [_undo_entry(workspace, model, entry) for entry in reversed(entries)]
    except IntegrityError:
        logger.warning("Undo of a data model revision collided in workspace %s", workspace.id)
        return None


def _model_class(object_type: str):
    return {DATASET: SemanticDataset, FIELD: SemanticField, RELATIONSHIP: SemanticRelationship}[
        object_type
    ]


def _current(entry: dict[str, Any]):
    return (
        _model_class(entry["object_type"])
        .objects.select_for_update()
        .filter(id=entry["object_uuid"])
        .first()
    )


def _conflict(entry: dict[str, Any], message: str) -> dict[str, Any]:
    return {
        "object": f"{entry['object_type']}/{entry['name']}",
        "object_uuid": entry["object_uuid"],
        "message": message,
    }


def _lock_model(workspace) -> SemanticModel | None:
    """None while a catalog refresh holds the model.

    Same no-wait lock a custom-dataset commit takes, so an undo never races a
    refresh that re-syncs the custom datasets it adds or removes.
    """
    try:
        # Savepoint: a refused NOWAIT aborts the transaction it runs in.
        with transaction.atomic():
            return SemanticModel.objects.select_for_update(nowait=True).get(workspace=workspace)
    except OperationalError as exc:
        if getattr(exc.__cause__, "sqlstate", None) == "55P03":
            return None
        raise


def _undo_conflict(workspace, model, entry, restoring: set[str], removing: set[str], refs):
    object_type = entry["object_type"]
    before, after = entry.get("before"), entry.get("after")
    current = _current(entry)
    if after is None:
        if current is not None:
            return _conflict(entry, "It exists again, so it cannot be restored.")
        return _restore_conflict(workspace, model, entry, before, restoring)
    if current is None:
        if _dataset_restored_first(entry, restoring):
            return None
        return _conflict(entry, "It was removed afterwards.")
    if before is None:
        now = snapshot_object(object_type, current, deep=True)
        if _authored(object_type, now) != _authored(object_type, after):
            return _conflict(entry, "It was edited afterwards, so removing it would lose that.")
        if user := _removal_user(refs, object_type, now, removing):
            return _conflict(entry, f"{user} uses it, so removing it would break that.")
        return None
    now = snapshot_object(object_type, current)
    changed = _changed_values(before, after)
    for key, (_old, new) in changed.items():
        if key != CURATED_KEY and _read(now, key) != new:
            return _conflict(entry, f"Its {key.removeprefix('metadata.')} changed afterwards.")
    if "name" in changed:
        return _rename_conflict(workspace, entry, before, after, removing, refs)
    return None


def _dataset_restored_first(entry: dict[str, Any], restoring: set[str]) -> bool:
    """A later entry of this revision deleted the object's dataset, cascading it away.

    Entries replay in reverse, so that dataset (with this object in its deep
    snapshot) is restored before this entry is undone.
    """
    after = entry["after"]
    if entry["object_type"] == FIELD:
        return after["dataset_id"] in restoring
    if entry["object_type"] == RELATIONSHIP:
        return bool({after["from_dataset_id"], after["to_dataset_id"]} & restoring)
    return False


def _removal_user(refs, object_type: str, now: dict[str, Any], removing: set[str]) -> str:
    if object_type == FIELD:
        return refs.user_of(now, removing)
    if object_type != DATASET:
        return ""
    fields = now.get("fields") or []
    going = {
        *removing,
        *(field["id"] for field in fields),
        *(relationship["id"] for relationship in now.get("relationships") or []),
    }
    for field in fields:
        if user := refs.user_of(field, going):
            return user
    return ""


def _rename_conflict(workspace, entry, before, after, removing: set[str], refs):
    old_name = before["name"]
    if entry["object_type"] == FIELD:
        taken = SemanticField.objects.filter(dataset_id=after["dataset_id"], name=old_name)
        if user := refs.user_of(after, removing):
            return _conflict(entry, f"{user} uses its current name, so renaming would break it.")
    elif entry["object_type"] == RELATIONSHIP:
        taken = SemanticRelationship.objects.filter(workspace=workspace, name=old_name)
    else:
        return None
    if taken.exclude(id=entry["object_uuid"]).exists():
        return _conflict(entry, f"Another {entry['object_type']} is now named '{old_name}'.")
    return None


class _References:
    """Every field's SQL text and every join, read once per undo on first use.

    The conflict pass writes nothing, so one read serves every entry.
    """

    def __init__(self, model) -> None:
        self._model = model

    @cached_property
    def _fields(self) -> list[tuple[str, str, str, str]]:
        rows = []
        fields = SemanticField.objects.filter(dataset__semantic_model=self._model).select_related(
            "dataset"
        )
        for field in fields:
            text = field_sql_text({**(field.metadata or {}), "expression": field.expression})
            rows.append((str(field.id), field.dataset.name, field.name, text))
        return rows

    @cached_property
    def _joins(self) -> list[tuple[str, str, str]]:
        return [
            (str(id_), name, normalize_member_references(expression or ""))
            for id_, name, expression in SemanticRelationship.objects.filter(
                workspace_id=self._model.workspace_id
            ).values_list("id", "name", "join_expression")
        ]

    def user_of(self, field_snapshot: dict[str, Any], removing: set[str]) -> str:
        """Name a field or join that references this field."""
        dataset, name = field_snapshot["dataset_name"], field_snapshot["name"]
        excluded = {*map(str, removing), str(field_snapshot["id"])}
        for field_id, field_dataset, field_name, text in self._fields:
            if field_id in excluded:
                continue
            if references_field(text, dataset, name, same_dataset=field_dataset == dataset):
                return f"Field {field_dataset}.{field_name}"
        for join_id, join_name, expression in self._joins:
            if join_id not in excluded and references_field(
                expression, dataset, name, same_dataset=False
            ):
                return f"Relationship {join_name}"
        return ""


def _restore_conflict(workspace, model, entry, before, restoring: set[str]):
    object_type = entry["object_type"]
    if object_type == DATASET:
        name = before["name"]
        taken = SemanticDataset.objects.filter(workspace=workspace, name=name).exists() or (
            CustomDataset.objects.filter(workspace=workspace, name=name).exists()
        )
        if taken:
            return _conflict(entry, f"Another dataset is now named '{name}'.")
        if before.get("custom_dataset"):
            try:
                _compiled_custom_sql(model, before["custom_dataset"])
            except CustomDatasetError:
                return _conflict(entry, "Its SQL no longer works on the current data.")
        for relationship in before.get("relationships", []):
            if problem := _relationship_restore_problem(workspace, relationship, restoring):
                return _conflict(entry, problem)
        return None
    if object_type == FIELD:
        if before["dataset_id"] in restoring:
            return None
        if not model.datasets.filter(id=before["dataset_id"]).exists():
            return _conflict(entry, "Its dataset no longer exists.")
        if SemanticField.objects.filter(
            dataset_id=before["dataset_id"], name=before["name"]
        ).exists():
            return _conflict(entry, f"Another field is now named '{before['name']}'.")
        return None
    problem = _relationship_restore_problem(workspace, before, restoring)
    return _conflict(entry, problem) if problem else None


def _relationship_restore_problem(workspace, snapshot, restoring: set[str]) -> str:
    endpoints = {snapshot["from_dataset_id"], snapshot["to_dataset_id"]} - restoring
    if SemanticDataset.objects.filter(id__in=endpoints).count() != len(endpoints):
        return f"A dataset joined by '{snapshot['name']}' no longer exists."
    if SemanticRelationship.objects.filter(workspace=workspace, name=snapshot["name"]).exists():
        return f"Another relationship is now named '{snapshot['name']}'."
    return ""


def _comparable(object_type: str, snapshot: dict[str, Any]) -> dict[str, Any]:
    if object_type == FIELD:
        return {
            **{column: snapshot.get(column) for column in FIELD_COLUMNS},
            **{
                f"metadata.{key}": _read(snapshot, f"metadata.{key}") or None
                for key in FIELD_AUTHORED_METADATA_KEYS
            },
        }
    return {
        "name": snapshot.get("name"),
        "from": snapshot.get("from_dataset_id"),
        "to": snapshot.get("to_dataset_id"),
        "type": snapshot.get("relationship_type"),
        "description": _read(snapshot, "metadata.description"),
    }


def _curated_values(snapshot: dict[str, Any], columns) -> dict[str, Any]:
    curated = _read(snapshot, CURATED_KEY) or []
    return {key: _read(snapshot, key if key in columns else f"metadata.{key}") for key in curated}


def _authored(object_type: str, snapshot: dict[str, Any]) -> dict[str, Any]:
    """The user-authored state of an object; refreshes rewrite everything else.

    A catalog refresh re-derives labels, generated fields and joins, so only
    canvas-created objects and curated keys show whether a person changed it.
    """
    if object_type != DATASET:
        return _authored_member(object_type, snapshot)
    fields = {
        field["id"]: authored
        for field in snapshot.get("fields") or []
        if (authored := _authored_member(FIELD, field))
    }
    relationships = {
        relationship["id"]: _comparable(RELATIONSHIP, relationship)
        for relationship in snapshot.get("relationships") or []
        if _read(relationship, "metadata.source") == CANVAS_SOURCE
    }
    return {
        "curated": _curated_values(snapshot, DATASET_COLUMNS),
        "fields": fields,
        "relationships": relationships,
    }


def _authored_member(object_type: str, snapshot: dict[str, Any]) -> dict[str, Any]:
    if object_type == RELATIONSHIP or _read(snapshot, "metadata.source") == CANVAS_SOURCE:
        return _comparable(object_type, snapshot)
    return _curated_values(snapshot, FIELD_COLUMNS)


def _changed_values(before: dict[str, Any], after: dict[str, Any]) -> dict[str, tuple]:
    """Keys the revision changed, with metadata split per key: ``{key: (old, new)}``."""
    changed: dict[str, tuple] = {}
    for key in set(before) | set(after):
        if key in {"id", "metadata", "fields", "relationships", "custom_dataset"}:
            continue
        if before.get(key) != after.get(key):
            changed[key] = (before.get(key), after.get(key))
    old_meta, new_meta = before.get("metadata") or {}, after.get("metadata") or {}
    for key in set(old_meta) | set(new_meta):
        if old_meta.get(key) != new_meta.get(key):
            changed[f"metadata.{key}"] = (old_meta.get(key), new_meta.get(key))
    return changed


def _read(snapshot: dict[str, Any], key: str):
    if key.startswith("metadata."):
        return (snapshot.get("metadata") or {}).get(key.removeprefix("metadata."))
    return snapshot.get(key)


def _undo_entry(workspace, model, entry: dict[str, Any]) -> dict[str, Any]:
    object_type = entry["object_type"]
    before, after = entry.get("before"), entry.get("after")
    current = _current(entry)
    if before is None:
        current_snapshot = snapshot_object(object_type, current, deep=True)
        _remove(object_type, current)
        return change_entry(object_type, entry["object_uuid"], DELETE, current_snapshot, None)
    if after is None:
        restored = _recreate(workspace, model, object_type, before)
        return change_entry(
            object_type,
            entry["object_uuid"],
            CREATE,
            None,
            snapshot_object(object_type, restored, deep=True),
        )
    current_snapshot = snapshot_object(object_type, current)
    metadata = dict(current.metadata or {})
    for key, (old, new) in _changed_values(before, after).items():
        if key == CURATED_KEY:
            # Keep curation that later revisions added; drop only this one's.
            added = set(new or []) - set(old or [])
            metadata["curated_fields"] = sorted(set(metadata.get("curated_fields", [])) - added)
        elif key.startswith("metadata."):
            meta_key = key.removeprefix("metadata.")
            if meta_key in (before.get("metadata") or {}):
                metadata[meta_key] = old
            else:
                metadata.pop(meta_key, None)
        else:
            setattr(current, key, old)
    current.metadata = metadata
    current.save()
    return change_entry(
        object_type,
        entry["object_uuid"],
        UPDATE,
        current_snapshot,
        snapshot_object(object_type, current),
    )


def _remove(object_type: str, obj) -> None:
    if object_type == DATASET:
        custom = obj.custom_dataset
        obj.delete()
        if custom is not None:
            custom.delete()
        return
    obj.delete()


def _recreate(workspace, model, object_type: str, snapshot: dict[str, Any]):
    if object_type == FIELD:
        return _create_field(snapshot)
    if object_type == RELATIONSHIP:
        return _create_relationship(workspace, snapshot)
    custom = None
    if custom_snapshot := snapshot.get("custom_dataset"):
        creator_id = custom_snapshot.get("created_by_id")
        if creator_id and not get_user_model().objects.filter(id=creator_id).exists():
            creator_id = None
        custom = CustomDataset.objects.create(
            id=custom_snapshot["id"],
            workspace=workspace,
            created_by_id=creator_id,
            **{column: custom_snapshot[column] for column in CUSTOM_DATASET_COLUMNS},
        )
    metadata = dict(snapshot.get("metadata") or {})
    if custom_snapshot:
        metadata["cube_sql"] = _compiled_custom_sql(model, custom_snapshot)
    dataset = SemanticDataset.objects.create(
        id=snapshot["id"],
        semantic_model=model,
        workspace=workspace,
        custom_dataset=custom,
        metadata=metadata,
        **{column: snapshot[column] for column in DATASET_COLUMNS},
    )
    for field in snapshot.get("fields") or []:
        _create_field(field)
    for relationship in snapshot.get("relationships") or []:
        _create_relationship(workspace, relationship)
    return dataset


def _compiled_custom_sql(model, custom_snapshot: dict[str, Any]) -> str:
    """Recompile against today's tables; the snapshot's SQL may predate a refresh."""
    return compile_custom_dataset_sql(
        custom_snapshot["definition_sql"],
        allowed_tables=allowed_custom_dataset_tables(model),
    )


def _create_field(snapshot: dict[str, Any]) -> SemanticField:
    return SemanticField.objects.create(
        id=snapshot["id"],
        dataset_id=snapshot["dataset_id"],
        metadata=snapshot.get("metadata") or {},
        **{column: snapshot[column] for column in FIELD_COLUMNS},
    )


def _create_relationship(workspace, snapshot: dict[str, Any]) -> SemanticRelationship:
    existing = SemanticRelationship.objects.filter(id=snapshot["id"]).first()
    if existing is not None:
        # Both endpoints of a join can be restored by one undo; the first creates it.
        return existing
    return SemanticRelationship.objects.create(
        id=snapshot["id"],
        workspace=workspace,
        from_dataset_id=snapshot["from_dataset_id"],
        to_dataset_id=snapshot["to_dataset_id"],
        metadata=snapshot.get("metadata") or {},
        **{column: snapshot[column] for column in RELATIONSHIP_COLUMNS},
    )
