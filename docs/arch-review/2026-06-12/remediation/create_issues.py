#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Create GitHub issues from issue-map.json (one per arch-review cluster).

DRY-RUN BY DEFAULT: prints every label/issue it would create and exits without
touching GitHub. Pass --execute to actually call `gh`. Review issue-map.json and
the dry-run output first.

  uv run create_issues.py                 # dry run (default)
  uv run create_issues.py --execute       # really create labels + issues
  uv run create_issues.py --execute --only refresh-data-loss recipe-runner-fix

Each cluster -> one issue: summary, a checklist of constituent findings, and per-
finding claim/chain/files (a ready-made TDD brief for an agent). A tracking meta-
issue links them all. Idempotency is best-effort: gh has no native upsert, so on a
re-run prefer --only for the keys you still need. Labels are create-if-missing.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ISSUE_MAP = HERE / "issue-map.json"

# label name -> color (hex, no '#'). Colors are cosmetic; gh ignores dupes.
LABEL_COLORS = {
    "wave:0": "5319e7", "wave:1": "b60205", "wave:2": "d93f0b", "wave:3": "fbca04",
    "tier:now": "b60205", "tier:next": "d93f0b", "tier:guardrail": "0e8a16",
    "tier:cleanup": "c2e0c6", "tier:design": "5319e7",
    "effort:S": "c5def5", "effort:M": "1d76db", "effort:L": "0052cc", "effort:XL": "0a3069",
    "design-gated": "5319e7", "status:broken-now": "b60205",
    "impact:data-loss": "b60205", "impact:security": "d93f0b",
    "impact:correctness": "fbca04", "impact:cost-perf": "0e8a16",
    "impact:velocity": "bfdadc",
    "arch-review-2026-06-12": "ededed",
}
TRACKING_LABEL = "arch-review-2026-06-12"


def run(cmd: list[str], *, dry: bool, capture: bool = False) -> str:
    printable = " ".join(c if " " not in c else f'"{c}"' for c in cmd)
    if dry:
        print(f"  [dry-run] {printable[:160]}")
        return ""
    res = subprocess.run(cmd, capture_output=capture, text=True)
    if res.returncode != 0:
        sys.stderr.write(res.stderr or "")
        raise SystemExit(f"command failed: {printable}")
    return (res.stdout or "").strip()


def ensure_labels(labels: set[str], *, dry: bool) -> None:
    print(f"\n== Labels ({len(labels)}) ==")
    for name in sorted(labels):
        color = LABEL_COLORS.get(name, "ededed")
        run(["gh", "label", "create", name, "--color", color, "--force"], dry=dry)


def issue_body(issue: dict) -> str:
    lines = [issue["summary"], ""]
    if issue["findings"]:
        lines.append(f"## Findings ({len(issue['findings'])})")
        lines.append("")
        for f in issue["findings"]:
            lines.append(
                f"- [ ] **`{f['id']}`** {f['status']}/{f['impact']} "
                f"(r{f['replication']}, {f['complexity']}) -- {f['title']}"
            )
        lines.append("")
        lines.append("<details><summary>Evidence (claim / chain / files)</summary>")
        lines.append("")
        for f in issue["findings"]:
            lines.append(f"### `{f['id']}` {f['title']}")
            lines.append(f"**Claim:** {f['claim']}")
            lines.append("")
            if f["chain"]:
                lines.append(f"**Chain:** `{f['chain']}`")
                lines.append("")
            if f["files"]:
                lines.append("**Files:** " + ", ".join(f"`{x}`" for x in f["files"]))
                lines.append("")
        lines.append("</details>")
        lines.append("")
    if issue.get("references"):
        lines.append("**Related issues:** " +
                     ", ".join(f"`{r}`" for r in issue["references"]))
        lines.append("")
    lines.append("---")
    lines.append(f"_Source: arch-review 2026-06-12 (repo HEAD 35e4230). "
                 f"Cluster `{issue['key']}`. See docs/arch-review/2026-06-12/synthesis.md._")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true",
                    help="actually create labels + issues (default: dry run)")
    ap.add_argument("--only", nargs="*", default=None,
                    help="restrict to these issue keys")
    ap.add_argument("--repo", default=None, help="owner/repo (default: gh current)")
    args = ap.parse_args()
    dry = not args.execute

    data = json.loads(ISSUE_MAP.read_text())
    issues = data["issues"]
    if args.only:
        issues = [i for i in issues if i["key"] in set(args.only)]
        if not issues:
            raise SystemExit(f"no issues matched --only {args.only}")

    repo_args = ["--repo", args.repo] if args.repo else []

    mode = "DRY RUN (no changes)" if dry else "EXECUTE (creating on GitHub)"
    print(f"=== create_issues.py -- {mode} ===")
    print(f"{len(issues)} issue(s) from {ISSUE_MAP.name}")

    all_labels: set[str] = {TRACKING_LABEL}
    for i in issues:
        all_labels.update(i["labels"])
    ensure_labels(all_labels, dry=dry)

    print(f"\n== Issues ({len(issues)}) ==")
    created: list[tuple[str, str]] = []
    for issue in issues:
        labels = sorted(set(issue["labels"]) | {TRACKING_LABEL})
        title = f"[arch] {issue['title']}"
        body = issue_body(issue)
        body_file = HERE / f".issue-body-{issue['key']}.md"
        print(f"\n- {issue['key']}  [{issue['effort']}]  labels: {','.join(labels)}")
        if dry:
            n = len(issue["findings"])
            print(f"    title: {title}")
            print(f"    body:  {len(body)} chars, {n} finding(s)")
        else:
            body_file.write_text(body)
            cmd = (["gh", "issue", "create", *repo_args, "--title", title,
                    "--body-file", str(body_file)]
                   + sum((["--label", l] for l in labels), []))
            url = run(cmd, dry=dry, capture=True)
            body_file.unlink(missing_ok=True)
            print(f"    created: {url}")
            created.append((issue["key"], url))

    # tracking meta-issue
    print("\n== Tracking meta-issue ==")
    meta_lines = [
        "Tracking issue for the 2026-06-12 architecture review remediation.",
        "",
        f"{data['total_findings']} findings -> {data['issue_count']} issues. "
        "Plan: docs/arch-review/2026-06-12/remediation-plan.md. "
        "Synthesis: docs/arch-review/2026-06-12/synthesis.md.",
        "",
    ]
    by_wave: dict[int, list[dict]] = {}
    for i in data["issues"]:
        by_wave.setdefault(i["wave"], []).append(i)
    url_by_key = dict(created)
    for wave in sorted(by_wave):
        meta_lines.append(f"### Wave {wave}")
        for i in by_wave[wave]:
            ref = url_by_key.get(i["key"], f"`{i['key']}`")
            gate = " **(design-gated)**" if i["design_gated"] else ""
            meta_lines.append(f"- [ ] {ref} -- {i['title']} [{i['effort']}]{gate}")
        meta_lines.append("")
    meta_body = "\n".join(meta_lines)
    if dry:
        print(f"  [dry-run] would create meta-issue ({len(meta_body)} chars, "
              f"{len(data['issues'])} links)")
    else:
        mf = HERE / ".issue-body-_tracking.md"
        mf.write_text(meta_body)
        url = run(["gh", "issue", "create", *repo_args,
                   "--title", "[arch] Remediation tracking -- 2026-06-12 review",
                   "--body-file", str(mf), "--label", TRACKING_LABEL], dry=dry, capture=True)
        mf.unlink(missing_ok=True)
        print(f"  created: {url}")

    if dry:
        print("\nDry run complete. Re-run with --execute to create on GitHub.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
