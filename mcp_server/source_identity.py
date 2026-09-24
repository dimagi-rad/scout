"""Provider-declared row identity facts, never guessed from a primary-key name."""


def source_identity(provider: str | None, table_name: str, columns: list[dict]) -> dict | None:
    if provider != "ocs" or table_name != "raw_messages":
        return None
    names = {column.get("name") for column in columns}
    guarded = {"message_id", "snapshot_revision", "message_version"} <= names
    return {
        "kind": "snapshot_local",
        "version": 2 if guarded else 1,
        "key": ["message_id"],
        "source_scope": "tenant",
        "scope_columns": ["session_id"] if "session_id" in names else [],
        "snapshot_column": "snapshot_revision" if guarded else None,
        "content_version_column": "message_version" if guarded else None,
        "safe_for_reviewed_labels": guarded,
        "invalidated_by": "Any change to a session's message history, including append.",
        "label_policy": (
            "Match the full message_id and message_version; keep unmatched rows unclassified. "
            "Never carry positional labels forward to another snapshot."
            if guarded
            else "Legacy positional IDs are unsafe for saved labels. Rematerialize messages with explicit authorization before reviewing labels."
        ),
    }


def unverified_source_identity() -> dict:
    return {
        "kind": "unknown",
        "version": None,
        "key": [],
        "source_scope": None,
        "scope_columns": [],
        "snapshot_column": None,
        "content_version_column": None,
        "safe_for_reviewed_labels": False,
        "invalidated_by": None,
        "label_policy": (
            "Source provenance is unverified; this is not evidence of unsafe positional IDs. "
            "Resolve provenance and the upstream identity contract before saving reviewed labels. "
            "Do not request a source refresh solely because provenance is unknown."
        ),
    }
