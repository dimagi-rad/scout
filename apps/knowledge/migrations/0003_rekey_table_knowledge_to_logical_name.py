"""Re-key TableKnowledge.table_name from physical schema-qualified names to
stable logical table names (arch #262, finding 01#5).

Historically the data-dictionary annotation endpoint stored annotations under
``{physical_schema}.{table}`` (e.g. ``commcare_xyz_r1a2b3c4.cases``). The
physical schema is regenerated on every refresh, so those rows orphaned all
annotations. We re-key them to the bare logical table name (``cases``) so
annotations survive refreshes and match the rows written by the column-note
generator (which already keyed by the logical name).

Collision handling: if re-keying would collide with an existing row for the
same ``(workspace, logical_name)`` (typically an auto-generated column-notes
row), we merge field-by-field — preferring the qualified row's non-empty
human-curated values — then delete the now-redundant qualified row.
"""

from django.db import migrations

# Fields merged when a qualified row collides with an existing logical-named row.
_TEXT_FIELDS = ("description", "owner", "refresh_frequency")
_LIST_FIELDS = ("use_cases", "data_quality_notes", "related_tables")
_DICT_FIELDS = ("column_notes",)


def _logical_name(table_name):
    return table_name.rsplit(".", 1)[-1]


def _merge_into(target, source):
    """Merge non-empty values from *source* into *target* in place.

    *source* is the qualified (human-curated) row; its non-empty values win.
    Returns the list of changed field names.
    """
    changed = []
    for field in _TEXT_FIELDS:
        src = getattr(source, field)
        if src and not getattr(target, field):
            setattr(target, field, src)
            changed.append(field)
    for field in _LIST_FIELDS:
        src = getattr(source, field) or []
        if src and not (getattr(target, field) or []):
            setattr(target, field, src)
            changed.append(field)
    for field in _DICT_FIELDS:
        src = getattr(source, field) or {}
        tgt = getattr(target, field) or {}
        if src:
            # Existing target keys win; fill gaps from source.
            merged = {**src, **tgt}
            if merged != tgt:
                setattr(target, field, merged)
                changed.append(field)
    return changed


def rekey_forward(apps, schema_editor):
    TableKnowledge = apps.get_model("knowledge", "TableKnowledge")

    qualified = list(TableKnowledge.objects.filter(table_name__contains="."))
    for row in qualified:
        logical = _logical_name(row.table_name)
        if logical == row.table_name:
            continue

        existing = (
            TableKnowledge.objects.filter(workspace=row.workspace, table_name=logical)
            .exclude(pk=row.pk)
            .first()
        )
        if existing is None:
            row.table_name = logical
            row.save(update_fields=["table_name"])
        else:
            changed = _merge_into(existing, row)
            if changed:
                existing.save(update_fields=changed)
            row.delete()


def rekey_backward(apps, schema_editor):
    # Logical names are not reversibly mappable to a physical schema (the
    # schema is regenerated on every refresh), so this is a no-op. Data is not
    # lost going forward → backward; the bare-named rows simply remain.
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("knowledge", "0002_initial"),
    ]

    operations = [
        migrations.RunPython(rekey_forward, rekey_backward),
    ]
