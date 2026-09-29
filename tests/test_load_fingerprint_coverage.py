"""The load fingerprint must hash every module that can change what a load writes.

A module missing from both halves of the fingerprint lets a deploy that edits only
that module reuse a published generation, or resume a FAILED candidate, built by
the old code. A raw-shaping module filed under the transform half is subtler: only
the raw half gates resume, so a deploy that edits it would resume a candidate whose
committed rows the new code would not write.

So the load path is walked in two parts. The raw walk starts at the materializer
(the entry point every load runs through) and stops at the transform entry points,
the modules the materializer hands off to for staging assets and the dbt run. Each
module it reaches must be in the raw half or excluded below, never in the
transform half. The transform walk starts at the transform entry points; what it
reaches may be in either half. A module both walks reach is held to the raw rule.

Orchestration reached only from apps/workspaces/tasks.py (candidate bookkeeping,
pipeline resolution, credentials) is out of scope: it decides when and where a
load runs, not what it writes, and hashing it would churn the fingerprint on
nearly every deploy.
"""

import ast
import pathlib

from apps.workspaces.services.load_generations import _RAW_LOAD_PATHS, _REPO_ROOT, _TRANSFORM_PATHS

_RAW_ENTRY_POINTS = ("mcp_server.services.materializer",)
# The materializer uses these only to regenerate staging assets (TransformationAsset
# rows, re-generated on every run) and to run dbt after the sources are loaded.
# Neither writes a raw table, so a resume that re-runs them loses nothing.
_TRANSFORM_ENTRY_POINTS = (
    "apps.transformations.services.commcare_staging",
    "apps.transformations.services.connect_staging",
    "apps.transformations.services.executor",
    "apps.transformations.services.staging_identity",
)
_FIRST_PARTY = ("apps", "config", "mcp_server")

# Modules the load path imports that cannot change the raw rows or transform
# output of a load. The walk does not descend into them: anything reached only
# through one of these is used only by it.
_NOT_LOAD_SHAPING = {
    "apps/common/error_codes.py": "Failure codes; decide how a failure is reported.",
    "apps/common/errors.py": "Exception types; decide how a failure is reported.",
    "apps/knowledge/services/__init__.py": "Re-exports KnowledgeRetriever for the agent.",
    "apps/knowledge/services/column_note_generator.py": (
        "Writes knowledge notes outside the tenant schema after the load."
    ),
    "apps/transformations/models.py": "ORM state; its schema changes ship as migrations.",
    "apps/users/models.py": "ORM state; its schema changes ship as migrations.",
    "apps/users/services/upstream_denial.py": "Records an upstream access denial.",
    "apps/workspaces/models.py": "ORM state; its schema changes ship as migrations.",
    "apps/workspaces/services/__init__.py": "Re-exports SchemaManager, excluded below.",
    "apps/workspaces/services/load_generations.py": "The fingerprint itself.",
    "apps/workspaces/services/schema_manager.py": (
        "Provisions and names schemas and roles; never decides their rows."
    ),
    "apps/workspaces/services/tenant_metadata.py": (
        "Membership visibility gate on stored metadata, not how it is loaded."
    ),
}


def _module_file(module: str) -> pathlib.Path | None:
    base = _REPO_ROOT.joinpath(*module.split("."))
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def _imported_modules(path: pathlib.Path, module: str) -> set[str]:
    package = module.split(".") if path.name == "__init__.py" else module.split(".")[:-1]
    found = set()
    for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            parts = package[: len(package) - node.level + 1] if node.level else []
            source = ".".join([*parts, node.module] if node.module else parts)
            found.add(source)
            # ``from pkg import submodule`` imports a module, not just a name.
            found.update(f"{source}.{alias.name}" for alias in node.names)
    # Importing a submodule also executes every ancestor package's __init__.py.
    for name in list(found):
        parts = name.split(".")
        found.update(".".join(parts[:depth]) for depth in range(1, len(parts)))
    return {name for name in found if name.split(".")[0] in _FIRST_PARTY}


def _relative(module: str) -> str | None:
    path = _module_file(module)
    return path.relative_to(_REPO_ROOT).as_posix() if path is not None else None


def _walk(entry_points, *, stop_at=frozenset()) -> set[str]:
    reached: set[str] = set()
    pending = list(entry_points)
    while pending:
        module = pending.pop()
        relative = _relative(module)
        if relative is None or relative in reached or relative in stop_at:
            continue
        reached.add(relative)
        if relative not in _NOT_LOAD_SHAPING:
            pending.extend(_imported_modules(_module_file(module), module))
    return reached


def _raw_load_files() -> set[str]:
    boundary = frozenset(_relative(module) for module in _TRANSFORM_ENTRY_POINTS)
    return _walk(_RAW_ENTRY_POINTS, stop_at=boundary)


def _transform_files() -> set[str]:
    return _walk(_TRANSFORM_ENTRY_POINTS)


def _load_path_files() -> set[str]:
    return _raw_load_files() | _transform_files()


def _has_code(relative: str) -> bool:
    """False for a file holding at most a docstring, such as an empty package init."""
    tree = ast.parse(_REPO_ROOT.joinpath(relative).read_text())
    return len(tree.body) > (ast.get_docstring(tree) is not None)


def _under(relative: str, roots) -> bool:
    return any(relative == root or relative.startswith(f"{root.rstrip('/')}/") for root in roots)


def _unaccounted(files, allowed_roots) -> list[str]:
    return sorted(
        path
        for path in files
        if _has_code(path) and not _under(path, allowed_roots) and path not in _NOT_LOAD_SHAPING
    )


def test_every_module_the_raw_load_reaches_is_in_the_raw_half():
    unaccounted = _unaccounted(_raw_load_files(), _RAW_LOAD_PATHS)
    assert not unaccounted, (
        "The raw load reaches these modules, but they are not in _RAW_LOAD_PATHS in "
        "apps/workspaces/services/load_generations.py. Add them there (not to "
        "_TRANSFORM_PATHS: only the raw half gates resume), or to _NOT_LOAD_SHAPING "
        f"here with a reason if they cannot change what a load writes: {unaccounted}"
    )


def test_every_module_the_transforms_reach_is_fingerprinted_or_excluded():
    unaccounted = _unaccounted(_transform_files(), (*_RAW_LOAD_PATHS, *_TRANSFORM_PATHS))
    assert not unaccounted, (
        "The transform phase reaches these modules, but they are not hashed into the "
        "load fingerprint. Add them to _TRANSFORM_PATHS in "
        "apps/workspaces/services/load_generations.py, or to _NOT_LOAD_SHAPING here "
        f"with a reason if they cannot change what a load writes: {unaccounted}"
    )


def test_transform_entry_points_are_the_raw_loads_direct_hand_off():
    # A boundary the raw load does not import directly would stop the raw walk
    # somewhere a raw-shaping module could hide behind it.
    direct = {
        _relative(name)
        for entry in _RAW_ENTRY_POINTS
        for name in _imported_modules(_module_file(entry), entry)
    }
    for module in _TRANSFORM_ENTRY_POINTS:
        relative = _relative(module)
        assert relative in direct, f"{module} is not imported by a raw entry point"
        assert _under(relative, _TRANSFORM_PATHS), f"{module} is not in _TRANSFORM_PATHS"


def test_the_halves_do_not_overlap():
    overlap = sorted(
        root
        for root in _TRANSFORM_PATHS
        if _under(root, _RAW_LOAD_PATHS) or any(_under(raw, (root,)) for raw in _RAW_LOAD_PATHS)
    )
    assert not overlap, f"Paths hashed in both halves: {overlap}"


def test_exclusions_are_still_on_the_load_path():
    stale = sorted(set(_NOT_LOAD_SHAPING) - _load_path_files())
    assert not stale, f"Drop exclusions the load path no longer imports: {stale}"


def test_exclusions_are_not_also_hashed():
    both = sorted(
        path for path in _NOT_LOAD_SHAPING if _under(path, (*_RAW_LOAD_PATHS, *_TRANSFORM_PATHS))
    )
    assert not both, f"Excluded modules are already fingerprinted: {both}"
