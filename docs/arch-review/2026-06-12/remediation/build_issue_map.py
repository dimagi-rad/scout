#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Build the finding->issue traceability map for the 2026-06-12 arch review.

Reads the verified-findings DB (../findings/batch-*.json), applies the cluster
definitions below (every finding assigned to exactly one primary issue), and emits:

  - issue-map.json : machine-readable, self-contained (per-finding claim/chain/files
                     inlined so create_issues.py needs nothing else)
  - backlog.md     : human-readable checklist grouped by wave

It HARD-FAILS if any of the 148 findings is unmapped or mapped twice. That coverage
assertion is the whole point: nothing from the review silently falls on the floor.

Run:  uv run build_issue_map.py        (or: python3 build_issue_map.py)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
FINDINGS_DIR = HERE.parent / "findings"

# --- Cluster definitions --------------------------------------------------------
# Each issue assigns its findings as PRIMARY. A finding lives in exactly one issue.
# `wave`: 0 = safety net first, 1 = stop active harm, 2 = structural, 3 = cleanup/tail.
# `design_gated`: needs a brainstorm -> spec -> plan pass (human product decision)
#                 BEFORE an agent writes code.
# `effort`: S (<1d), M (days), L (1-2wk), XL (multi-wk) -- from synthesis Section 5
#           where given, else estimated.

CLUSTERS: list[dict] = [
    # ---- WAVE 0: make the safety net real ----
    {
        "key": "ci-deploy-integrity",
        "title": "CI runs the real-DB regression suites and deploys gate on green",
        "wave": 0, "tier": "guardrail", "effort": "M", "design_gated": False,
        "summary": (
            "CI sets neither DATABASE_URL nor MANAGED_DATABASE_URL, so every real-DB "
            "incident-regression suite is silently skipped under a green badge; deploys "
            "are not gated on tests. Until this lands, no fix below is actually verified "
            "by the suite. Also: makemigrations --check, .dockerignore."
        ),
        "findings": ["12#2", "10#3", "08#4", "08#3"],
    },
    {
        "key": "chat-mcp-contract-test",
        "title": "Real (unmocked) chat<->MCP contract test + frontend test infra",
        "wave": 0, "tier": "guardrail", "effort": "M", "design_gated": False,
        "summary": (
            "Stand up FastMCP in-process with a real client, no mocks, on the highest-"
            "churn seam. Would have caught the recipe breakage, toolCallId mismatch, "
            "get_metadata card bug, and onboarding 404 as a CLASS. Includes frontend "
            "unit-test infra and retiring the mocks that pin dead seams."
        ),
        "findings": ["10#4", "10#5", "12#0", "02#6", "07#0"],
    },

    # ---- WAVE 1: stop active harm (BROKEN-NOW data-loss + security) ----
    {
        "key": "identifier-minting-helper",
        "title": "One identifier helper: 63-byte + collision guard on every minted name",
        "wave": 1, "tier": "now", "effort": "M", "design_gated": False,
        "summary": (
            "The #227 fix was applied to view names only. Build ONE helper "
            "(length/collision/sanitization, keyed by (provider, external_id)) that "
            "schema, role, refresh, dbt model/alias names all route through. Closes the "
            "cross-tenant collision class -- same bug family as the 2026-06-10 incident."
        ),
        "findings": ["00#3", "00#4", "04#6"],
    },
    {
        "key": "refresh-data-loss",
        "title": "Data Dictionary refresh destroys the data it just loaded",
        "wave": 1, "tier": "now", "effort": "M", "design_gated": False,
        "summary": (
            "ACTIVELY DESTROYS PROD DATA ON EVERY CLICK. Cheapest correct fix: route the "
            "button to materialize_workspace (in-place reload + sibling rebuilds) and "
            "delete the _r-schema machinery. Put this FIRST in wave 1."
        ),
        "findings": ["00#0", "00#9"],
    },
    {
        "key": "mcp-teardown-and-state-cas",
        "title": "MCP teardown_schema updates Django state; add CAS at the drop site",
        "wave": 1, "tier": "now", "effort": "M", "design_gated": False,
        "summary": (
            "The agent-exposed teardown tool drops physical schemas but never updates "
            "Django state and cascade-kills sibling workspaces; the queued teardown task "
            "drops resurrected rows with no state CAS. Fix or unbind the tool; add the CAS."
        ),
        "findings": ["00#2", "03#0"],
    },
    {
        "key": "recipe-runner-fix",
        "title": "Recipe runner signature drift -- feature 100% dead since March",
        "wave": 1, "tier": "now", "effort": "S", "design_gated": False,
        "summary": (
            "RecipeRunner calls build_agent_graph with a removed kwarg -> TypeError -> 500 "
            "-> RecipeRun stranded RUNNING forever. Triple drift (kwarg + initial-state + "
            "result-extraction). Restores the feature to working-as-designed; the "
            "redesign question is separate (see content-satellite-redesign)."
        ),
        "findings": ["00#1"],
    },
    {
        "key": "artifact-sandbox-isolation",
        "title": "Artifact sandbox is a no-op (allow-scripts + allow-same-origin)",
        "wave": 1, "tier": "now", "effort": "S", "design_gated": False,
        "summary": (
            "Drop allow-same-origin from the artifact iframe (runtime-verified to restore "
            "isolation). Closes prompt-injection -> session takeover: agent-generated code "
            "can currently issue credentialed state-changing requests as the viewer."
        ),
        "findings": ["02#1"],
    },
    {
        "key": "artifact-multitenant-render",
        "title": "Multi-tenant live artifacts query the wrong schema / show zero artifacts",
        "wave": 1, "tier": "now", "effort": "S", "design_gated": False,
        "summary": (
            "Route artifact query-data through load_workspace_context (view schema, not "
            "first tenant's schema); populate Artifact.conversation_id so shared/public "
            "threads stop showing zero artifacts and the dead render_url is fixed."
        ),
        "findings": ["00#6", "00#8"],
    },
    {
        "key": "dbt-transformations",
        "title": "dbt runs arbitrary user SQL as managed-DB superuser; generated models fail",
        "wave": 1, "tier": "now", "effort": "M", "design_gated": False,
        "summary": (
            "Confine dbt: dedicated low-privilege role + search_path (or gate transform "
            "writes behind validation). Same change fixes the generated CommCare staging "
            "models that fail silently, and the workspace-scope transforms that never run."
        ),
        "findings": ["04#3", "04#4", "04#5"],
    },
    {
        "key": "onboarding-apikey-404",
        "title": "Onboarding 'Use an API Key' POSTs to a deleted endpoint (guaranteed 404)",
        "wave": 1, "tier": "now", "effort": "S", "design_gated": False,
        "summary": "First-run path for every non-OAuth user 404s. Re-point to the live endpoint.",
        "findings": ["04#7"],
    },
    {
        "key": "high-blast-one-liners",
        "title": "Three one-liners with outsized blast radius",
        "wave": 1, "tier": "now", "effort": "S", "design_gated": False,
        "summary": (
            "Fail-CLOSED thread-ownership except (currently fails open -> foreign thread "
            "append); resume-prompt else-branch honesty (stops telling the agent a FAILED "
            "run 'just completed'); reconciler staleness measured against the resume job "
            "(stops falsely failing healthy long materializations)."
        ),
        "findings": ["06#8", "14#5", "02#9"],
    },
    {
        "key": "mcp-metadata-disclosure",
        "title": "Cross-tenant metadata disclosure via unqualified pg_catalog reads",
        "wave": 1, "tier": "now", "effort": "M", "design_gated": False,
        "summary": (
            "pg_catalog is world-readable regardless of SET ROLE and is advertised in the "
            "system prompt; tenant schema names are customer identifiers and reltuples "
            "leaks row counts. (SET ROLE does block actual cross-tenant DATA reads.)"
        ),
        "findings": ["09#0"],
    },
    {
        "key": "ocs-team-scope",
        "title": "OCS participants sync is team-wide, not chatbot-scoped",
        "wave": 1, "tier": "now", "effort": "M", "design_gated": False,
        "summary": (
            "The 'chatbot' param Scout sends is documented upstream but unimplemented, so "
            "whole-team rosters land in a single-chatbot tenant schema. Plus: team-mismatch "
            "currently surfaces as a generic 'No credential configured'."
        ),
        "findings": ["12#3", "07#3"],
    },
    {
        "key": "frontend-tool-cards",
        "title": "Live/reload tool-output rich cards broken (toolCallId, 0-tables, truncation)",
        "wave": 1, "tier": "now", "effort": "M", "design_gated": False,
        "summary": (
            "toolCallId mismatch kills per-card progress/Stop live; get_metadata renders "
            "'0 tables' on reload (Array.isArray over a map); 2000-char live truncation "
            "breaks success cards live; thinking blocks dropped on reload; error-envelope "
            "info discarded; the apostrophe->double-quote parse hack."
        ),
        "findings": ["06#3", "13#3", "13#4", "05#2", "13#5", "13#6", "13#7", "13#8"],
    },
    {
        "key": "frontend-workspace-switch",
        "title": "Pages don't refetch / clear state on workspace switch",
        "wave": 1, "tier": "now", "effort": "S", "design_gated": False,
        "summary": (
            "Artifacts/Recipes show stale cross-workspace data then 404 (threadId fix reset "
            "only threadId); ConnectionsPage guard compares the wrong ids (never fires); "
            "WorkspaceDetailPage never clears a prior load error."
        ),
        "findings": ["04#9", "05#3", "05#5"],
    },
    {
        "key": "base-path-and-labs",
        "title": "BASE_PATH-bypassing URLs break the labs /scout deployment + widget SDK",
        "wave": 1, "tier": "now", "effort": "M", "design_gated": False,
        "summary": (
            "Root-relative URLs break health poll, sandbox iframe, public share pages on "
            "labs; widget setMode/theme are no-ops and widget.js isn't routed; "
            "DEPLOY_ENVIRONMENT mislabels labs as development. (Labs infra is out-of-repo.)"
        ),
        "findings": ["04#8", "06#6", "11#1", "08#5"],
    },

    # ---- WAVE 2: structural consolidations (design-gated where noted) ----
    {
        "key": "multitenant-retrofit-shim",
        "title": "Finish the single-tenant -> multi-tenant retrofit (first-tenant shim)",
        "wave": 2, "tier": "next", "effort": "L", "design_gated": True,
        "summary": (
            "DESIGN-GATED (what SHOULD a multi-tenant workspace show?). The first-tenant "
            "compat shim silently drives Data Dictionary, refresh, knowledge, recipe TTL; "
            "never-materialized multi-tenant workspaces spin forever; zero-tenant "
            "workspaces dead-end in chat."
        ),
        "findings": ["00#7", "05#4", "06#5"],
    },
    {
        "key": "permission-layer",
        "title": "One permission layer enforced on the content surface",
        "wave": 2, "tier": "next", "effort": "L", "design_gated": True,
        "summary": (
            "DESIGN-GATED (what should READ/RW/MANAGE actually permit? should recipe "
            "is_shared/is_public exist at all?). Today DRF permission classes have zero "
            "importers; READ members mutate artifacts/knowledge/recipes and drive "
            "destructive agent tools. Honor archived_at uniformly; fix dead role tests."
        ),
        "findings": ["00#5", "05#1", "06#7", "01#7", "12#1"],
    },
    {
        "key": "status-catalog-module",
        "title": "One status/catalog module (single source of world-state truth)",
        "wave": 2, "tier": "next", "effort": "L", "design_gated": True,
        "summary": (
            "DESIGN-GATED (define the canonical shape). Status/catalog derived ~7 ways "
            "with user-visible divergence -> the #190 panic-loop input class. Single "
            "derivation for status; single table-catalog used by prompt+tools+API; write "
            "MATERIALIZING or delete it (15 readers, 0 writers); fix get_schema_status's "
            "dead shape; reconcile the 3 metadata read-scopes; fix fail-open dbt catalog."
        ),
        "findings": ["09#6", "02#2", "03#4", "03#5", "01#9", "09#8", "09#7"],
    },
    {
        "key": "credential-lifetime-long-jobs",
        "title": "Credential lifetime for long jobs (CommCare 15-min OAuth TTL)",
        "wave": 2, "tier": "next", "effort": "L", "design_gated": False,
        "summary": (
            "Any CommCare-OAuth materialization >15 min is structurally impossible today: "
            "one credential snapshot per run, no mid-run/401 refresh, stale-token fallback, "
            "no 'reconnect your account' mapping. Plus CommCare/OCS retry hardening, "
            "uncancellable Retry-After sleep, and refresh-revokes-running-token race."
        ),
        "findings": ["14#3", "14#4", "12#4", "14#6", "14#7", "03#6", "09#3"],
    },
    {
        "key": "mcp-hardening",
        "title": "MCP server hardening: caller auth, connection hygiene, pooling",
        "wave": 2, "tier": "next", "effort": "M", "design_gated": False,
        "summary": (
            "No caller authentication: tenant-scoped tools trust workspace_id blindly. Add "
            "shared-secret auth + membership checks; add dead-DB-connection hygiene (the "
            "22-hour-outage class, fixed only for the worker); pool the managed-DB "
            "connection; delete the dead OAuth-token plumbing paid for every chat turn."
        ),
        "findings": ["01#6", "08#0", "10#1", "01#0"],
    },
    {
        "key": "cost-latency-floor",
        "title": "Cost/latency floor: prompt caching, history + knowledge budgets, polling",
        "wave": 2, "tier": "next", "effort": "M", "design_gated": False,
        "summary": (
            "No Anthropic prompt caching anywhere; prune_messages is dead (history replayed "
            "unbounded, ~quadratic lifetime cost + eventual context overflow); knowledge "
            "injected with no budget; serial per-table TLS connections per cache miss; "
            "always-on polling; in-memory knowledge pagination; per-version artifact copies; "
            "OCS page_size; uncached me_view re-hitting all providers; DD N+1s."
        ),
        "findings": ["02#3", "01#3", "01#4", "13#1", "06#2", "05#6", "05#7", "09#9", "10#2", "07#4"],
    },
    {
        "key": "background-work-robustness",
        "title": "Background-work robustness: concurrency, janitors, per-tenant locking",
        "wave": 2, "tier": "next", "effort": "L", "design_gated": False,
        "summary": (
            "One worker at concurrency 1 serializes all background work; no janitor owns "
            "MaterializationRun after worker death (zombie 'doing' jobs); no per-tenant "
            "mutual exclusion; ThreadJob races its own dispatch; cancel semantics diverge; "
            "FutureApp current_app siblings; TTL rewind; checkpointer pool race; resume vs "
            "live turn unserialized; purge orphans; the dependency graph has no owner; "
            "stream 300s timeout only checked between events; job/checkpoint tables unpruned."
        ),
        "findings": [
            "08#2", "03#9", "03#3", "10#0", "01#2", "01#1", "07#1", "02#5", "04#0",
            "03#1", "03#2", "08#1", "06#9", "09#4", "09#5", "02#8",
        ],
    },
    {
        "key": "truthful-failure",
        "title": "Truthful failure: stop rendering errors as success",
        "wave": 2, "tier": "next", "effort": "M", "design_gated": False,
        "summary": (
            "SSE errors become text with finishReason 'stop'; checkpointer/thread-list "
            "outages return []-with-200; cascade-FAILED rows report a fabricated cause + "
            "wrong recovery advice; login-resolution failures swallowed (user lands with no "
            "data, no error); panic-loop escalation never streamed live."
        ),
        "findings": ["06#4", "07#7", "07#9", "07#6", "06#1"],
    },
    {
        "key": "observability",
        "title": "Minimum observability: alarms, real health checks, destruction logging",
        "wave": 2, "tier": "next", "effort": "M", "design_gated": False,
        "summary": (
            "Zero CloudWatch alarms; static health check; no worker/MCP heartbeat; "
            "production audit log suppressed (INFO under root WARNING) with empty "
            "project_id; DROP SCHEMA CASCADE is silent on success (the 2026-06-10 forensic "
            "question is still unanswerable); share tokens/OAuth codes in access logs."
        ),
        "findings": ["08#7", "08#8", "08#9", "08#6"],
    },
    {
        "key": "auth-perimeter-hardening",
        "title": "Close the second auth perimeter (stock allauth /accounts/)",
        "wave": 2, "tier": "next", "effort": "M", "design_gated": False,
        "summary": (
            "/accounts/ HTML auth is a live second registration perimeter: open self-"
            "registration, bypasses the custom rate limiter, login-CSRF/silent linking on "
            "GET (SOCIALACCOUNT_LOGIN_ON_GET + AUTO_CONNECT), no working email backend "
            "(kills password reset), OCS allowlist open by default."
        ),
        "findings": ["13#9", "14#0", "14#1", "14#2", "07#2"],
    },
    {
        "key": "account-merge-correctness",
        "title": "Account-merge correctness (privilege propagation, metadata cascade)",
        "wave": 2, "tier": "next", "effort": "M", "design_gated": False,
        "summary": (
            "merge_users OR-propagates is_staff/is_superuser from the deleted duplicate "
            "(invisible at the y/N prompt -- plausibly already prod state); the verified-"
            "email merge gate is subtle; conflict paths cascade-delete the duplicate's "
            "TenantMetadata (live + historical migration 0004)."
        ),
        "findings": ["11#4", "01#8", "04#1", "11#9"],
    },
    {
        "key": "admin-lockdown",
        "title": "Django admin lockdown + management-command fixes",
        "wave": 2, "tier": "next", "effort": "M", "design_gated": False,
        "summary": (
            "Admin is an unguarded privileged-write surface: editable state-machine rows "
            "re-arm DROP SCHEMA CASCADE, unthrottled login, plaintext tokens, self-"
            "escalation; registration inverted (dangerous rows editable, operator models "
            "absent); AgentLearningAdmin renders escaped HTML; setup_oauth_apps composes "
            "wrong env names; backfill_readonly_roles aborts on first drift."
        ),
        "findings": ["11#3", "11#5", "11#6", "11#7", "11#8"],
    },
    {
        "key": "infra-network-security",
        "title": "Infra/network security: credential separation, subnets, egress, CI role",
        "wave": 2, "tier": "next", "effort": "L", "design_gated": False,
        "summary": (
            "One DB + one master-superuser credential for both planes; RDS/Redis in public "
            "subnets, SSH 0.0.0.0/0, admin internet-exposed; wide-open egress + IMDSv1 "
            "(loader SSRF -> instance-role theft); CI deploy role reads every RDS master "
            "password in the account. (Some items are prod-stack facts, not in-repo.)"
        ),
        "findings": ["11#0", "11#2", "10#8", "10#9"],
    },
    {
        "key": "knowledge-fixes",
        "title": "Knowledge / Data Dictionary correctness fixes",
        "wave": 2, "tier": "next", "effort": "M", "design_gated": False,
        "summary": (
            "TableKnowledge keyed by physical schema name (every refresh orphans "
            "annotations; multi-tenant can never match); autosave silently wipes "
            "related_tables; import 500s + round-trip loses duplicate-titled entries; "
            "learning lifecycle inert while the prompt implies usage. Candidate inputs to "
            "content-satellite-redesign."
        ),
        "findings": ["01#5", "05#0", "05#8", "05#9"],
    },

    # ---- WAVE 3: tail (LATENT/DEBT/COSMETIC), remaining guardrails, cleanup ----
    {
        "key": "provider-data-quality",
        "title": "Provider loader data-quality + upstream-contract fixes",
        "wave": 3, "tier": "next", "effort": "M", "design_gated": False,
        "summary": (
            "Unguarded inbound payloads (>255 names, missing keys, NUMERIC overflow); dead/"
            "wrong columns (raw_visits.images, participant_platform, unstable message_id, "
            "Connect count always None); next-URL trust (Connect plaintext http://); "
            "resumability split across two registries; offset-skip (Forms v0.5 only); "
            "denominator accuracy >10k."
        ),
        "findings": [
            "02#7", "12#8", "12#9", "13#0", "12#7", "13#2", "09#2", "12#5",
            "09#1", "12#6", "03#7",
        ],
    },
    {
        "key": "reference-drift-detection",
        "title": "Reference-drift detection janitor (stored SQL/table refs)",
        "wave": 3, "tier": "guardrail", "effort": "M", "design_gated": False,
        "summary": (
            "No drift detection for any stored schema reference (artifact SQL, knowledge, "
            "learnings, recipes); every rename mechanism ships without migrating refs. Add "
            "a janitor that validates stored refs against live catalogs and flags rather "
            "than silently rotting."
        ),
        "findings": ["06#0"],
    },
    {
        "key": "checkpoint-retention-privacy",
        "title": "LangGraph checkpoint retention + deletion on member removal",
        "wave": 3, "tier": "next", "effort": "M", "design_gated": False,
        "summary": (
            "Member removal deletes Thread rows but never LangGraph checkpoints; "
            "checkpoints are never pruned anywhere (retention/privacy gap + unbounded "
            "growth)."
        ),
        "findings": ["02#4"],
    },
    {
        "key": "dead-code-cleanup",
        "title": "Dead code / rename residue / cosmetic drift cleanup",
        "wave": 3, "tier": "cleanup", "effort": "M", "design_gated": False,
        "summary": (
            "Dead DRF permission classes, dual checkpointer module (+ MemorySaver-in-prod "
            "footgun), project_id audit residue, domainSlice naming stratum, export 501, "
            "dead share surface, vestigial RecipeStep, execute_async; stale Celery "
            "docstrings, removed-model prompt sections, doubled heading, inline imports; "
            "TS type lies; minor run-lifecycle drift; the two REFUTED findings (kept for "
            "the record)."
        ),
        "findings": ["02#0", "10#6", "10#7", "03#8", "07#8", "07#5", "04#2"],
    },
]

# Design epics that span issues above (no primary findings of their own; they
# coordinate a product decision before the constituent fixes are designed).
DESIGN_EPICS: list[dict] = [
    {
        "key": "content-satellite-redesign",
        "title": "[DESIGN] Content-satellite redesign: recipes + knowledge + artifacts",
        "wave": 2, "tier": "design", "effort": "L", "design_gated": True,
        "summary": (
            "The wave-1/2 fixes restore recipes/knowledge/artifacts to working-AS-DESIGNED. "
            "Whether that current design is what we want is a separate product question. "
            "Brainstorm -> spec before committing to repair-in-place vs rethink. References "
            "recipe-runner-fix, knowledge-fixes, artifact-multitenant-render, "
            "artifact-sandbox-isolation, permission-layer (recipe privacy)."
        ),
        "references": [
            "recipe-runner-fix", "knowledge-fixes", "artifact-multitenant-render",
            "artifact-sandbox-isolation", "permission-layer",
        ],
        "findings": [],
    },
]

# Process guardrails (policy, not code; tracked so they aren't forgotten).
PROCESS_GUARDRAILS: list[dict] = [
    {
        "key": "sibling-sweep-policy",
        "title": "[POLICY] Sibling-sweep as fix policy on every incident-fix PR",
        "wave": 1, "tier": "guardrail", "effort": "S", "design_gated": False,
        "summary": (
            "Pattern 1 ('fixed-where-it-bit') is the single most predictive finding "
            "generator: every fix PR must list the grep for sibling sites and either fix "
            "or explicitly tick them off."
        ),
        "references": ["identifier-minting-helper"],
        "findings": [],
    },
    {
        "key": "rerun-review-quarterly",
        "title": "[POLICY] Re-run the arch-review coverage matrix quarterly",
        "wave": 3, "tier": "guardrail", "effort": "S", "design_gated": False,
        "summary": (
            "The methodology is repeatable; diff the next findings DB against this run "
            "(2026-06-12) to measure remediation and catch drift."
        ),
        "references": [],
        "findings": [],
    },
]


def load_findings() -> dict[str, dict]:
    """Return {id -> record} for all findings, id = 'NN#i'."""
    out: dict[str, dict] = {}
    for path in sorted(FINDINGS_DIR.glob("batch-*.json")):
        batch = path.stem.split("batch-")[1]
        for idx, rec in enumerate(json.loads(path.read_text())):
            out[f"{batch}#{idx}"] = rec
    return out


def labels_for(issue: dict, findings: list[dict]) -> list[str]:
    labels = [f"tier:{issue['tier']}", f"wave:{issue['wave']}", f"effort:{issue['effort']}"]
    if issue.get("design_gated"):
        labels.append("design-gated")
    impacts = {f.get("impact") for f in findings if f.get("impact")}
    for imp in sorted(impacts):
        labels.append(f"impact:{imp}")
    statuses = {f.get("status") for f in findings}
    if "BROKEN-NOW" in statuses:
        labels.append("status:broken-now")
    return labels


def main() -> int:
    findings = load_findings()
    all_ids = set(findings)

    # --- coverage assertion ---
    assigned: dict[str, str] = {}
    errors: list[str] = []
    for issue in CLUSTERS:
        for fid in issue["findings"]:
            if fid not in all_ids:
                errors.append(f"{issue['key']}: references unknown finding {fid}")
            if fid in assigned:
                errors.append(f"{fid} double-mapped: {assigned[fid]} and {issue['key']}")
            assigned[fid] = issue["key"]

    unmapped = sorted(all_ids - set(assigned))
    if unmapped:
        errors.append(f"UNMAPPED findings ({len(unmapped)}): {', '.join(unmapped)}")

    if errors:
        print("COVERAGE CHECK FAILED:", file=sys.stderr)
        for e in errors:
            print("  -", e, file=sys.stderr)
        return 1

    print(f"COVERAGE OK: {len(assigned)}/{len(all_ids)} findings mapped to "
          f"{len(CLUSTERS)} issues; no duplicates.")

    # --- build issue-map.json ---
    out_issues = []
    for issue in CLUSTERS + DESIGN_EPICS + PROCESS_GUARDRAILS:
        recs = [findings[fid] | {"id": fid} for fid in issue["findings"]]
        out_issues.append({
            "key": issue["key"],
            "title": issue["title"],
            "wave": issue["wave"],
            "tier": issue["tier"],
            "effort": issue["effort"],
            "design_gated": issue.get("design_gated", False),
            "labels": labels_for(issue, recs) if recs else
                      [f"tier:{issue['tier']}", f"wave:{issue['wave']}",
                       f"effort:{issue['effort']}"] +
                      (["design-gated"] if issue.get("design_gated") else []),
            "summary": issue["summary"],
            "references": issue.get("references", []),
            "findings": [
                {
                    "id": r["id"],
                    "title": r.get("title", ""),
                    "status": r.get("status", ""),
                    "impact": r.get("impact", ""),
                    "complexity": r.get("complexity", ""),
                    "replication": r.get("replication", 0),
                    "files": r.get("files", []),
                    "chain": r.get("chain", ""),
                    "claim": r.get("claim", ""),
                }
                for r in recs
            ],
        })

    payload = {
        "generated_from": "docs/arch-review/2026-06-12/findings/batch-00..14.json",
        "repo_head": "35e4230",
        "review_date": "2026-06-12",
        "total_findings": len(all_ids),
        "issue_count": len(CLUSTERS),
        "design_epics": [e["key"] for e in DESIGN_EPICS],
        "process_guardrails": [g["key"] for g in PROCESS_GUARDRAILS],
        "issues": out_issues,
    }
    (HERE / "issue-map.json").write_text(json.dumps(payload, indent=2) + "\n")
    print(f"wrote issue-map.json ({len(out_issues)} issues incl. epics/policies)")

    # --- build backlog.md ---
    wave_names = {
        0: "Wave 0 -- make the safety net real",
        1: "Wave 1 -- stop active harm (BROKEN-NOW data-loss + security)",
        2: "Wave 2 -- structural consolidations",
        3: "Wave 3 -- tail, remaining guardrails, cleanup",
    }
    lines = [
        "# Scout arch-review remediation backlog",
        "",
        f"Generated from the 2026-06-12 review (repo HEAD 35e4230). "
        f"{len(all_ids)} findings -> {len(CLUSTERS)} issues "
        f"(+{len(DESIGN_EPICS)} design epics, {len(PROCESS_GUARDRAILS)} policies).",
        "",
        "Do not hand-edit -- regenerate with `uv run build_issue_map.py`.",
        "",
    ]
    for wave in (0, 1, 2, 3):
        wave_issues = [i for i in CLUSTERS + DESIGN_EPICS + PROCESS_GUARDRAILS
                       if i["wave"] == wave]
        if not wave_issues:
            continue
        lines += [f"## {wave_names[wave]}", ""]
        for issue in wave_issues:
            recs = [findings[fid] | {"id": fid} for fid in issue["findings"]]
            tags = []
            if issue.get("design_gated"):
                tags.append("**DESIGN-GATED**")
            if any(r.get("status") == "BROKEN-NOW" for r in recs):
                tags.append("BROKEN-NOW")
            tagstr = f" [{', '.join(tags)}]" if tags else ""
            n = len(issue["findings"])
            fcount = f" ({n} finding{'s' if n != 1 else ''})" if n else ""
            lines.append(f"### {issue['title']}  `{issue['key']}` "
                         f"[{issue['effort']}]{tagstr}{fcount}")
            lines.append("")
            lines.append(issue["summary"])
            lines.append("")
            for r in recs:
                lines.append(
                    f"- [ ] `{r['id']}` {r.get('status','')}/{r.get('impact','')} "
                    f"r{r.get('replication',0)} -- {r.get('title','')}"
                )
            if issue.get("references"):
                lines.append("")
                lines.append("References: " +
                             ", ".join(f"`{x}`" for x in issue["references"]))
            lines.append("")
    (HERE / "backlog.md").write_text("\n".join(lines) + "\n")
    print("wrote backlog.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
