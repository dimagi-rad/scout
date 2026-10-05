"""Named phases of a load after its sources are fetched, shown on the chat's progress card.

Written as ``MaterializationRun.progress["phase"]``; the frontend maps each value
to a plain-language title, so renaming one is a frontend change too.
"""

from enum import StrEnum


class LoadPhase(StrEnum):
    BUILDING_TABLES = "building_tables"
    CHECKING_QUALITY = "checking_quality"
    COMBINING_SITES = "combining_sites"
    BUILDING_MODEL = "building_model"
    FINISHING = "finishing"
    ANSWERING = "answering"
