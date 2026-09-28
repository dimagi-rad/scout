"""The load fingerprint must hash every module that can change what a load writes.

A module missing from ``_IMPLEMENTATION_PATHS`` lets a deploy that edits only that
module reuse a published generation, or resume a FAILED candidate, built by the
old code. This walks the static first-party imports of the materializer (the
entry point every load runs through) and requires each module it reaches to be
hashed or explicitly excluded below with a reason.
"""

import ast
import pathlib

from apps.workspaces.services.load_generations import _IMPLEMENTATION_PATHS, _REPO_ROOT

_ENTRY_POINT = "mcp_server.services.materializer"
_FIRST_PARTY = ("apps", "config", "mcp_server")

# Modules the load path imports that cannot change the raw rows or transform
# output of a load. The walk does not descend into them: anything reached only
# through one of these is used only by it.
_NOT_LOAD_SHAPING = {
    "apps/common/error_codes.py": "Failure codes; decide how a failure is reported.",
    "apps/common/errors.py": "Exception types; decide how a failure is reported.",
    "apps/knowledge/services/column_note_generator.py": (
        "Writes knowledge notes outside the tenant schema after the load."
    ),
    "apps/transformations/models.py": "ORM state; its schema changes ship as migrations.",
    "apps/users/models.py": "ORM state; its schema changes ship as migrations.",
    "apps/users/services/upstream_denial.py": "Records an upstream access denial.",
    "apps/workspaces/models.py": "ORM state; its schema changes ship as migrations.",
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
    return {name for name in found if name.split(".")[0] in _FIRST_PARTY}


def _load_path_files() -> set[str]:
    reached: set[str] = set()
    pending = [_ENTRY_POINT]
    while pending:
        module = pending.pop()
        path = _module_file(module)
        if path is None:
            continue
        relative = path.relative_to(_REPO_ROOT).as_posix()
        if relative in reached:
            continue
        reached.add(relative)
        if relative not in _NOT_LOAD_SHAPING:
            pending.extend(_imported_modules(path, module))
    return reached


def _is_hashed(relative: str) -> bool:
    return any(
        relative == root or relative.startswith(f"{root.rstrip('/')}/")
        for root in _IMPLEMENTATION_PATHS
    )


def test_every_module_on_the_load_path_is_fingerprinted_or_excluded():
    unaccounted = sorted(
        path
        for path in _load_path_files()
        if not _is_hashed(path) and path not in _NOT_LOAD_SHAPING
    )
    assert not unaccounted, (
        "These modules are imported by the load path but not hashed into the load "
        "fingerprint. Add them to _IMPLEMENTATION_PATHS in "
        "apps/workspaces/services/load_generations.py, or to _NOT_LOAD_SHAPING here "
        f"with a reason if they cannot change what a load writes: {unaccounted}"
    )


def test_exclusions_are_still_on_the_load_path():
    stale = sorted(set(_NOT_LOAD_SHAPING) - _load_path_files())
    assert not stale, f"Drop exclusions the load path no longer imports: {stale}"


def test_exclusions_are_not_also_hashed():
    both = sorted(path for path in _NOT_LOAD_SHAPING if _is_hashed(path))
    assert not both, f"Excluded modules are already fingerprinted: {both}"
