# /// script
# requires-python = ">=3.11"
# dependencies = ["markdown"]
# ///
"""Build a standalone interactive HTML report from the Scout architecture-review artifacts.

Reads the verified-findings DB, coverage logs, synthesis narrative and methodology doc
under docs/arch-review/<date>/ and emits a single self-contained HTML file (no runtime
network dependencies) with an interactive findings explorer + the full synthesis.

Run:  uv run docs/arch-review/report-site/build.py
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import markdown

ROOT = Path(__file__).resolve().parents[3]
DATE = "2026-06-12"
SRC = ROOT / "docs" / "arch-review" / DATE
HERE = Path(__file__).resolve().parent
TEMPLATE = HERE / "template.html"
OUT = HERE / f"scout-arch-review-{DATE}.html"
HEAD = "35e4230"

# Findings not placed by the §3 auto-extraction (refuted ones live in Appendix B;
# 07#3/07#6 are referenced only from §5/§4 prose).
SUBSYSTEM_OVERRIDES = {
    "04#2": "Refuted (Appendix B)",
    "07#5": "Refuted (Appendix B)",
    "07#3": "3.7 Provider loaders & upstream contracts",
    "07#6": "3.2 Tenancy, identity, authz",
}

# ─────────────────────────────────────────────────────────────────────────────
# Load findings
# ─────────────────────────────────────────────────────────────────────────────
def load_findings() -> list[dict]:
    out = []
    batches = sorted((SRC / "findings").glob("batch-*.json"))
    for path in batches:
        batch = int(re.search(r"batch-(\d+)", path.name).group(1))
        for idx, item in enumerate(json.loads(path.read_text())):
            ver = item.get("verification", {}) or {}
            out.append({
                "id": f"{batch:02d}#{idx}",
                "title": item.get("title", ""),
                "claim": item.get("claim", ""),
                "chain": item.get("chain", ""),
                "status": item.get("status", ""),
                "impact": item.get("impact", ""),
                "complexity": item.get("complexity", ""),
                "replication": item.get("replication", 1),
                "reviewers": item.get("reviewers", []),
                "files": item.get("files", []),
                "verdict": ver.get("verdict", ""),
                "votes": ver.get("votes", []),
            })
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Subsystem mapping from synthesis §3
# ─────────────────────────────────────────────────────────────────────────────
def build_subsystem_map(synthesis: str) -> tuple[dict[str, str], list[str]]:
    m = re.search(r"\n## 3\. Findings by subsystem(.*?)\n## 4\.", synthesis, re.S)
    sec3 = m.group(1)
    parts = re.split(r"\n### (3\.\d+[^\n]*)", sec3)
    submap: dict[str, str] = {}
    order: list[str] = []
    for i in range(1, len(parts), 2):
        # Shorten the header: drop parenthetical asides for the tag label.
        head = re.sub(r"\s*\(.*?\)\s*$", "", parts[i].strip())
        body = parts[i + 1]
        order.append(head)
        for fid in re.findall(r"(\d{2}#\d+)", body):
            submap.setdefault(fid, head)
    submap.update(SUBSYSTEM_OVERRIDES)
    if "Refuted (Appendix B)" not in order:
        order.append("Refuted (Appendix B)")
    return submap, order


# ─────────────────────────────────────────────────────────────────────────────
# Markdown → HTML with finding-reference linkification
# ─────────────────────────────────────────────────────────────────────────────
def render_md(md_text: str) -> str:
    html = markdown.markdown(
        md_text,
        extensions=["tables", "fenced_code", "sane_lists", "attr_list"],
    )
    return linkify_refs(html)


_CODE_RE = re.compile(r"(<pre.*?</pre>|<code.*?</code>)", re.S)
_REF_RE = re.compile(r"(?<![\w#])(\d{2}#\d+)\b")


def linkify_refs(html: str) -> str:
    """Turn finding ids like 00#0 into clickable refs, skipping <pre>/<code> regions."""
    def repl(text: str) -> str:
        return _REF_RE.sub(
            lambda m: f'<a class="ref" data-fid="{m.group(1)}">{m.group(1)}</a>', text
        )

    pieces = _CODE_RE.split(html)
    # Even indices are outside code; odd indices are code spans we leave untouched.
    return "".join(p if i % 2 else repl(p) for i, p in enumerate(pieces))


def section(synthesis: str, start: str, end: str | None) -> str:
    """Extract a `## ` section body (without its own H2 header) from the synthesis."""
    if end:
        pat = re.escape(start) + r"(.*?)" + re.escape(end)
        m = re.search(pat, synthesis, re.S)
        body = m.group(1)
    else:
        body = synthesis.split(start, 1)[1]
    return body.strip()


# ─────────────────────────────────────────────────────────────────────────────
# Build
# ─────────────────────────────────────────────────────────────────────────────
def main() -> None:
    findings = load_findings()
    synthesis = (SRC / "synthesis.md").read_text()
    methodology = (ROOT / "docs" / "arch-review-methodology.md").read_text()

    submap, order = build_subsystem_map(synthesis)
    for f in findings:
        f["subsystem"] = submap.get(f["id"], "Other / cross-cutting")
    if "Other / cross-cutting" not in order:
        order.append("Other / cross-cutting")

    data = {
        "findings": findings,
        "subsystemsOrder": order,
        "meta": {"date": DATE, "head": HEAD},
    }
    data_json = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")

    # Narrative sections (rendered to HTML, refs linkified).
    overview_md = (
        section(synthesis, "## 1. Executive summary", "## 2. As-built")
        + "\n\n## As-built architecture map\n\n"
        + section(synthesis, "## 2. As-built architecture map", "## 3. Findings")
    )
    overview_html = render_md(overview_md)
    subsystems_html = render_md(
        "# Findings by subsystem\n\n*The synthesizer's grouped narrative. Every "
        "`NN#i` reference is a clickable link into the findings explorer.*\n\n"
        + section(synthesis, "## 3. Findings by subsystem", "## 4. Cross-cutting")
    )
    patterns_html = render_md(
        "# Cross-cutting patterns\n\n"
        + section(synthesis, "## 4. Cross-cutting patterns", "## 5. Prioritized")
    )
    recs_html = render_md(
        "# Prioritized recommendations\n\n"
        + section(synthesis, "## 5. Prioritized recommendations", "## 6. What's")
    )
    whatsfine_html = render_md(
        "# What's actually fine\n\n"
        + section(synthesis, "## 6. What's actually fine", "## 7. Coverage")
    )
    coverage_html = render_md(
        "# Coverage & confidence\n\n"
        + section(synthesis, "## 7. Coverage appendix", None)
    )
    methodology_html = render_md(methodology)

    template = TEMPLATE.read_text()
    replacements = {
        "{{DATE}}": DATE,
        "{{HEAD}}": HEAD,
        "{{NFINDINGS}}": str(len(findings)),
        "{{DATA_JSON}}": data_json,
        "{{OVERVIEW}}": overview_html,
        "{{METHODOLOGY}}": methodology_html,
        "{{SUBSYSTEMS}}": subsystems_html,
        "{{PATTERNS}}": patterns_html,
        "{{RECOMMENDATIONS}}": recs_html,
        "{{WHATSFINE}}": whatsfine_html,
        "{{COVERAGE}}": coverage_html,
    }
    for k, v in replacements.items():
        template = template.replace(k, v)

    OUT.write_text(template)
    size_kb = OUT.stat().st_size / 1024
    print(f"✓ wrote {OUT.relative_to(ROOT)}  ({size_kb:.0f} KB)")
    print(f"  {len(findings)} findings across {len(order)} subsystem groups")
    unmapped = [f["id"] for f in findings if f["subsystem"] == "Other / cross-cutting"]
    if unmapped:
        print(f"  unmapped → Other: {unmapped}")


if __name__ == "__main__":
    main()
