"""Guards the hand-maintained copies of the graph vocabulary in Python and TypeScript.

The backend validator (graph_doc.py) and the frontend renderer (recharts.tsx,
blocks.tsx) each carry their own allowlists. They are compared as source text so
a one-sided edit fails here instead of surfacing as a document the validator
accepts but the renderer rejects (or the reverse).
"""

import re
from pathlib import Path

import pytest

from apps.artifacts.services import graph_doc

REPO_ROOT = Path(__file__).resolve().parent.parent
GRAPH_DIR = REPO_ROOT / "frontend" / "src" / "components" / "ArtifactGraph"
MANAGER_AGENT = REPO_ROOT / "apps" / "agents" / "tools" / "artifact_manager_agent.py"

STRING = re.compile(r'"((?:[^"\\]|\\.)*)"')


def _read(name: str) -> str:
    return (GRAPH_DIR / name).read_text()


def _balanced(source: str, open_index: int) -> str:
    """Return the text inside the bracket at ``open_index`` (string-literal aware)."""
    pairs = {"(": ")", "[": "]", "{": "}"}
    stack = []
    i = open_index
    while i < len(source):
        ch = source[i]
        if ch == '"':
            i = STRING.match(source, i).end()
            continue
        if ch in pairs:
            stack.append(pairs[ch])
        elif stack and ch == stack[-1]:
            stack.pop()
            if not stack:
                return source[open_index + 1 : i]
        i += 1
    raise AssertionError("unbalanced brackets while parsing TypeScript source")


def _after(source: str, anchor: str) -> int:
    match = re.search(anchor, source)
    assert match, f"TypeScript anchor not found (source layout changed?): {anchor!r}"
    return match.end()


def _strings(body: str) -> set[str]:
    return {m.group(1) for m in STRING.finditer(body)}


def _string_set(source: str, name: str) -> set[str]:
    """Strings in ``const NAME = new Set([...])`` (optionally typed)."""
    start = _after(source, rf"const {name}\b[^=]*=\s*new Set(?:<[^>]*>)?\(\s*(?=\[)")
    values = _strings(_balanced(source, start))
    assert values, f"parsed no strings for TypeScript set {name}"
    return values


def _ts_prop_allowlist(source: str) -> dict[str, set[str]]:
    start = _after(source, r"const RECHARTS_PROP_ALLOWLIST\b[^=]*=\s*(?=\{)")
    body = _balanced(source, start)
    result = {}
    for match in re.finditer(r"^  (\w+): new Set\(\s*(?=\[)", body, re.MULTILINE):
        result[match.group(1)] = _strings(_balanced(body, match.end()))
    assert result, "parsed no entries from TypeScript RECHARTS_PROP_ALLOWLIST"
    assert all(result.values()), "a TypeScript prop allowlist entry parsed as empty"
    return result


def _ts_registry_kinds(source: str) -> set[str]:
    start = _after(source, r"const RECHARTS_REGISTRY\b[^=]*=\s*(?=\{)")
    kinds = set(re.findall(r"\b([A-Z]\w+)\b", _balanced(source, start)))
    assert kinds, "parsed no component kinds from TypeScript RECHARTS_REGISTRY"
    return kinds


def _ts_palettes(source: str) -> dict[str, list[str]]:
    start = _after(source, r"export const CHART_PALETTES\s*=\s*(?=\{)")
    body = _balanced(source, start)
    palettes = {}
    for match in re.finditer(r"^  (\w+): (?=\[)", body, re.MULTILINE):
        palettes[match.group(1)] = list(STRING.findall(_balanced(body, match.end())))
    assert palettes, "parsed no palettes from TypeScript CHART_PALETTES"
    assert all(palettes.values()), "a TypeScript palette parsed as empty"
    return palettes


def _ts_block_types(source: str) -> set[str]:
    start = _after(
        source, r"function buildStoryRegistry\([^)]*\)[^{]*\{\s*const specs: BlockSpec\[\] = (?=\[)"
    )
    body = _balanced(source, start)
    # Top-level specs only: nested `type:` keys sit deeper than the 6-space spec indent.
    kinds = set(re.findall(r'^      type: "(\w+)",$', body, re.MULTILINE))
    assert kinds, "parsed no block types from TypeScript buildStoryRegistry"
    return kinds


@pytest.fixture(scope="module")
def recharts_source() -> str:
    return _read("recharts.tsx")


@pytest.fixture(scope="module")
def blocks_source() -> str:
    return _read("blocks.tsx")


def test_recharts_component_kinds_match(recharts_source):
    ts_kinds = _ts_registry_kinds(recharts_source)
    assert len(ts_kinds) >= 18
    assert ts_kinds == graph_doc.RECHARTS_COMPONENT_TYPES


def test_recharts_prop_allowlists_match(recharts_source):
    ts = _ts_prop_allowlist(recharts_source)
    py = graph_doc.RECHARTS_PROP_ALLOWLIST
    assert set(ts) == set(py), "components with prop allowlists differ"
    for kind in sorted(py):
        assert ts[kind] == py[kind], f"{kind} prop allowlist differs between TS and Python"


def test_recharts_data_and_result_key_sets_match(recharts_source):
    assert _string_set(recharts_source, "DATA_INJECT_TYPES") == graph_doc.RECHARTS_DATA_TYPES
    assert _string_set(recharts_source, "RESULT_KEY_PROPS") == graph_doc.RECHARTS_RESULT_KEY_PROPS
    assert _string_set(recharts_source, "COLOR_PROPS") == graph_doc.RECHARTS_COLOR_PROPS


def test_palette_colors_match(recharts_source):
    palettes = _ts_palettes(recharts_source)
    ts_colors = {color for colors in palettes.values() for color in colors}
    assert ts_colors == graph_doc.SAFE_RECHARTS_COLORS
    assert set(palettes) == graph_doc.GRAPH_STYLE_KEYS["palette"]


def test_compact_chart_types_match(recharts_source):
    start = _after(recharts_source, r'if \(!(?=\["line")')
    assert set(STRING.findall(_balanced(recharts_source, start))) == graph_doc.COMPACT_CHART_TYPES


def test_block_kinds_match(blocks_source):
    ts_kinds = _ts_block_types(blocks_source)
    assert len(ts_kinds) >= 11
    assert ts_kinds == graph_doc.KNOWN_BLOCK_TYPES
    assert set(graph_doc.CONFIG_KEYS) == graph_doc.KNOWN_BLOCK_TYPES


def test_agent_prompt_lists_every_block_config_key():
    prompt = MANAGER_AGENT.read_text()
    start = prompt.index("Block types and config keys:")
    end = prompt.index("How to build data-backed blocks:")
    section = prompt[start:end]
    entries = re.split(r"\n(?=- `)", section)
    by_block = {m.group(1): e for e in entries if (m := re.match(r"- `(\w+)`", e))}
    assert set(by_block) == graph_doc.KNOWN_BLOCK_TYPES
    for block, keys in graph_doc.CONFIG_KEYS.items():
        listed = set(re.findall(r"`config\.(\w+)`", by_block[block]))
        assert keys <= listed, f"{block}: prompt omits config keys {sorted(keys - listed)}"
