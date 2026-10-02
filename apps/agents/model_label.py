import re

_MODEL_ID = re.compile(
    r"^claude-(?P<family>opus|sonnet|haiku)-(?P<major>\d+)-(?P<minor>\d+)(?:-(?:\d{8}|latest))?$"
)


def model_display_name(model_id: str) -> str:
    """Short friendly name for a Claude model id, e.g. ``claude-opus-5-5`` -> ``Opus 5.5``.

    Unrecognised ids come back unchanged so the label never hides what actually runs.
    """
    match = _MODEL_ID.match(model_id.strip())
    if not match:
        return model_id
    return f"{match['family'].capitalize()} {match['major']}.{match['minor']}"
