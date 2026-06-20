"""The trigger-agnostic engine for cross-opp canonical measures.

Spec in -> resolve across the workspace's opps -> classify doubt -> commit (additive
model regen + lineage + Cube reload) or hand back for approval. Fed by both the
on-demand agent tool and the app-driven proposer.
"""

from __future__ import annotations

from dataclasses import asdict

from apps.transformations.services.measure_resolver import MeasureResolution

_DOUBT_STATUSES = frozenset({"low_confidence", "absent"})


def serialize_resolution(r: MeasureResolution) -> dict:
    return asdict(r)


def deserialize_resolution(d: dict) -> MeasureResolution:
    return MeasureResolution(**d)


def classify_doubt(
    resolutions: dict[str, MeasureResolution],
) -> tuple[bool, list[str]]:
    """Doubt = any opp the resolver was unsure about (low_confidence) or found absent."""
    flagged = [opp for opp, r in resolutions.items() if r.status in _DOUBT_STATUSES]
    return (bool(flagged), flagged)
