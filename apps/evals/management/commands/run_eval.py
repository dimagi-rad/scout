"""Eval runner command: seed golden questions, execute both answer paths, print scorecard.

Usage:
    python manage.py run_eval --workspace <workspace_id> [--preagg] [--runs N]

LIVE STACK REQUIRED
-------------------
This command requires all of the following to be running and configured:

  * Django API server (with ANTHROPIC_API_KEY, DATABASE_URL set)
  * MCP server (default: http://localhost:8100/mcp)
  * Cube (CUBE_SQL_HOST, CUBE_SQL_PORT, CUBE_REST_URL, CUBEJS_API_SECRET set)
  * The workspace must have a completed materialization run (data available)

It will NOT work in unit tests. For pure unit tests of the scorecard, see
tests/test_eval_scorecard.py which injects fake EvalRun objects.

What the command does
---------------------
1. Load (upsert) seed GoldenQuery records from
   apps/evals/fixtures/golden_questions.yml into the given workspace.
2. For each GoldenQuery, call run_eval() N times (default 3) with real
   free_path and cube_path callables:
   - free_path: LLM writes a SQL SELECT, executes via MCP execute_query.
   - cube_path: Fetches semantic catalog, LLM writes Semantic SQL, executes via
     semantic_query against the Cube SQL API.
3. Aggregate all EvalRun records and print the scorecard (correctness %,
   consistency, mean latency, Cube answerability, pre-agg speedup).
4. Write a JSON report to docs/superpowers/evals/<workspace>-<timestamp>.json.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

import yaml
from asgiref.sync import async_to_sync
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage

from apps.evals.models import EvalRun, GoldenQuery
from apps.evals.services.runner import run_eval
from apps.evals.services.scorecard import summarize
from apps.users.models import Tenant
from apps.workspaces.models import (
    SchemaState,
    TenantSchema,
    Workspace,
    WorkspaceViewSchema,
)
from mcp_server.context import load_workspace_context
from mcp_server.pipeline_registry import registry
from mcp_server.services.metadata import pipeline_list_tables, workspace_list_tables
from mcp_server.services.query import execute_query
from mcp_server.services.semantic import semantic_catalog, semantic_query

logger = logging.getLogger(__name__)

# Path to the seed fixture file (relative to this command file)
_FIXTURE_PATH = Path(__file__).parent.parent.parent / "fixtures" / "golden_questions.yml"

# Report output dir (relative to repo root, mirroring gstack conventions)
_REPORT_DIR = Path(__file__).parent.parent.parent.parent.parent / "docs" / "superpowers" / "evals"


# ---------------------------------------------------------------------------
# LLM helper
# ---------------------------------------------------------------------------


def _llm() -> ChatAnthropic:
    """Build the default ChatAnthropic client."""
    return ChatAnthropic(
        model=settings.DEFAULT_LLM_MODEL,
        max_tokens=2048,
    )


async def _call_llm(client: Any, system: str, user: str) -> str:
    """Invoke the LLM; prefer ainvoke, fall back to sync in thread."""
    messages = [SystemMessage(content=system), HumanMessage(content=user)]
    if hasattr(client, "ainvoke"):
        response = await client.ainvoke(messages)
    else:
        response = await asyncio.to_thread(client.invoke, messages)
    if hasattr(response, "content"):
        content = response.content
        if isinstance(content, list):
            return " ".join(
                block.get("text", "") if isinstance(block, dict) else str(block)
                for block in content
            )
        return str(content)
    return str(response)


# ---------------------------------------------------------------------------
# Free-SQL path
# ---------------------------------------------------------------------------

_FREE_SYSTEM = """\
You are a SQL expert.  Write a single read-only SELECT statement that answers
the user's question exactly.  Return ONLY the SQL — no explanation, no markdown
fences, no trailing semicolons.  Use table and column names as they appear in
the provided schema.
"""


def _build_free_user_prompt(question: str, table_list: list[dict]) -> str:
    tables_txt = "\n".join(
        f"  - {t['name']}: {t.get('description', '(no description)')}" for t in table_list
    )
    return (
        f"Available tables:\n{tables_txt}\n\n"
        f"Question: {question}\n\n"
        "Write a SELECT query that answers this question."
    )


async def _resolve_table_list(workspace_id: str, ctx: Any) -> list[dict]:
    """Resolve the table list for a workspace from the metadata service."""
    is_view_schema = await WorkspaceViewSchema.objects.filter(
        schema_name=ctx.schema_name, state=SchemaState.ACTIVE
    ).aexists()
    if is_view_schema:
        return await workspace_list_tables(ctx)

    ts = await TenantSchema.objects.filter(schema_name=ctx.schema_name).afirst()
    if ts is None:
        return []

    tenant = await Tenant.objects.aget(id=ts.tenant_id)
    pipeline_config = registry.get_by_provider(tenant.provider) or registry.get("commcare_sync")
    if pipeline_config is None:
        return []
    return await pipeline_list_tables(ts, pipeline_config)


async def make_free_path(workspace_id: str, client: Any) -> Any:
    """Return an async free_path callable for the given workspace."""
    ctx = await load_workspace_context(workspace_id)
    table_list = await _resolve_table_list(workspace_id, ctx)

    async def free_path(question: str) -> dict:
        prompt = _build_free_user_prompt(question, table_list)
        t0 = time.monotonic()
        sql = (await _call_llm(client, _FREE_SYSTEM, prompt)).strip()
        # Remove trailing semicolons that the validator rejects
        sql = sql.rstrip(";").strip()
        result_envelope = await execute_query(ctx, sql)
        ms = int((time.monotonic() - t0) * 1000)
        return {"sql": sql, "result": result_envelope, "ms": ms}

    return free_path


# ---------------------------------------------------------------------------
# Cube semantic path
# ---------------------------------------------------------------------------

_CUBE_SYSTEM = """\
You are an analytics engineer specialising in Cube.js Semantic SQL.
Given the available cubes, measures, and dimensions, write a Semantic SQL
SELECT that answers the user's question.  Use MEASURE(...) and DIMENSION(...)
syntax.  Return ONLY the SQL — no explanation, no markdown fences.

Example:
    SELECT MEASURE(orders.revenue), DIMENSION(orders.status)
    FROM orders
"""


def _build_cube_user_prompt(question: str, catalog: dict) -> str:
    cubes = catalog.get("cubes", [])
    catalog_txt_parts = []
    for cube in cubes:
        measures = ", ".join(m["name"] for m in cube.get("measures", []))
        dimensions = ", ".join(d["name"] for d in cube.get("dimensions", []))
        catalog_txt_parts.append(
            f"  Cube: {cube['name']}\n"
            f"    Measures:   {measures or '(none)'}\n"
            f"    Dimensions: {dimensions or '(none)'}"
        )
    catalog_txt = "\n".join(catalog_txt_parts) or "  (no cubes available)"
    return (
        f"Available Cubes:\n{catalog_txt}\n\n"
        f"Question: {question}\n\n"
        "Write a Semantic SQL query that answers this question."
    )


async def make_cube_path(workspace_id: str, client: Any) -> Any:
    """Return an async cube_path callable for the given workspace."""
    catalog = await semantic_catalog(workspace_id)

    async def cube_path(question: str) -> dict:
        prompt = _build_cube_user_prompt(question, catalog)
        t0 = time.monotonic()
        sem_sql = (await _call_llm(client, _CUBE_SYSTEM, prompt)).strip()
        result_envelope = await semantic_query(sem_sql, workspace_id)
        ms = int((time.monotonic() - t0) * 1000)
        return {"query": sem_sql, "result": result_envelope, "ms": ms}

    return cube_path


# ---------------------------------------------------------------------------
# Fixture loading (upsert seed GoldenQuery records)
# ---------------------------------------------------------------------------


async def upsert_seed_questions(workspace: Workspace) -> list[GoldenQuery]:
    """Load/upsert seed GoldenQuery records from the YAML fixture."""
    if not _FIXTURE_PATH.exists():
        raise CommandError(f"Fixture file not found: {_FIXTURE_PATH}")

    with _FIXTURE_PATH.open() as fh:
        questions = yaml.safe_load(fh) or []

    golden_queries = []
    for item in questions:
        title = item.get("title", "").strip()
        if not title:
            continue
        gq, _ = await GoldenQuery.objects.aupdate_or_create(
            workspace=workspace,
            title=title,
            defaults={
                "question": item.get("question", "").strip(),
                "reference_sql": item.get("reference_sql", "").strip(),
                "expected_summary": item.get("expected_summary", "").strip(),
                "source": item.get("source", "seed"),
            },
        )
        golden_queries.append(gq)

    return golden_queries


# ---------------------------------------------------------------------------
# Async orchestration
# ---------------------------------------------------------------------------


async def _run_async(
    workspace_id: str,
    *,
    use_preagg: bool,
    runs: int,
    stdout_write: Any,
) -> tuple[list[EvalRun], dict]:
    """Main async body: upsert, evaluate, summarize."""
    try:
        workspace = await Workspace.objects.aget(id=workspace_id)
    except Workspace.DoesNotExist as exc:
        raise CommandError(f"Workspace '{workspace_id}' not found") from exc

    stdout_write(f"Loading seed golden questions for workspace {workspace_id}…")
    golden_queries = await upsert_seed_questions(workspace)
    stdout_write(f"  {len(golden_queries)} golden question(s) upserted.")

    client = _llm()

    stdout_write("Building answer paths…")
    try:
        free_p = await make_free_path(workspace_id, client)
    except Exception as exc:
        raise CommandError(f"Could not build free_path: {exc}") from exc

    try:
        cube_p = await make_cube_path(workspace_id, client)
    except Exception as exc:
        raise CommandError(f"Could not build cube_path: {exc}") from exc

    all_runs: list[EvalRun] = []
    total = len(golden_queries)
    for i, gq in enumerate(golden_queries, 1):
        stdout_write(f"  [{i}/{total}] Evaluating: {gq.title!r} ({runs} run(s))…")
        try:
            eval_runs = await run_eval(
                gq,
                runs=runs,
                use_preagg=use_preagg,
                free_path=free_p,
                cube_path=cube_p,
            )
            all_runs.extend(eval_runs)
        except Exception as exc:
            stdout_write(f"    WARNING: failed for {gq.title!r}: {exc!r}")
            logger.warning("run_eval failed for GoldenQuery %s: %r", gq.pk, exc)

    card = summarize(all_runs)
    return all_runs, card


# ---------------------------------------------------------------------------
# Management command
# ---------------------------------------------------------------------------


class Command(BaseCommand):
    help = (
        "Seed golden questions and run the free-SQL vs Cube evaluation suite.\n\n"
        "LIVE STACK REQUIRED: This command calls the LLM (ANTHROPIC_API_KEY) and "
        "Cube SQL API (CUBE_SQL_HOST/PORT, CUBEJS_API_SECRET).  It will NOT run "
        "without a live environment.  For unit tests, see tests/test_eval_scorecard.py."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--workspace",
            required=True,
            help="Workspace UUID to run the eval against.",
        )
        parser.add_argument(
            "--preagg",
            action="store_true",
            default=False,
            help="Mark runs as using pre-aggregations (sets used_preaggregation=True).",
        )
        parser.add_argument(
            "--runs",
            type=int,
            default=3,
            help="Number of independent runs per golden question (default: 3).",
        )

    def handle(self, *args: Any, **opts: Any) -> None:
        workspace_id = opts["workspace"]
        use_preagg = opts["preagg"]
        runs = opts["runs"]

        self.stdout.write(
            self.style.MIGRATE_HEADING(
                f"Scout Eval — workspace={workspace_id}  preagg={use_preagg}  runs={runs}"
            )
        )

        # Drive the async orchestration from the sync handle() via async_to_sync.
        # This is a management command (not a view), so async_to_sync is appropriate here.
        try:
            all_runs, card = async_to_sync(_run_async)(
                workspace_id,
                use_preagg=use_preagg,
                runs=runs,
                stdout_write=self.stdout.write,
            )
        except CommandError:
            raise
        except Exception as exc:
            raise CommandError(f"Eval run failed: {exc}") from exc

        # ------------------------------------------------------------------
        # Print scorecard
        # ------------------------------------------------------------------
        self._print_scorecard(card)

        # ------------------------------------------------------------------
        # Write JSON report
        # ------------------------------------------------------------------
        _REPORT_DIR.mkdir(parents=True, exist_ok=True)
        ts_label = timezone.now().strftime("%Y%m%d-%H%M%S")
        report_path = _REPORT_DIR / f"{workspace_id}-{ts_label}.json"
        report = {
            "workspace_id": workspace_id,
            "preagg": use_preagg,
            "runs_per_question": runs,
            "generated_at": timezone.now().isoformat(),
            "total_eval_runs": len(all_runs),
            "scorecard": card,
        }
        report_path.write_text(json.dumps(report, indent=2))
        self.stdout.write(self.style.SUCCESS(f"\nReport written to: {report_path}"))

    def _print_scorecard(self, card: dict) -> None:
        self.stdout.write("\n" + "=" * 60)
        self.stdout.write(self.style.MIGRATE_HEADING("SCORECARD"))
        self.stdout.write("=" * 60)

        c = card["correctness"]
        self.stdout.write(
            f"Correctness:      {c['correct']}/{c['total']} correct "
            f"({c['pct']:.1f}%)" if c["pct"] is not None else "Correctness:      N/A"
        )

        cons = card["consistency"]
        mean_ag = cons["mean_agreement"]
        self.stdout.write(
            f"Consistency:      mean agreement {mean_ag:.1%}"
            if mean_ag is not None
            else "Consistency:      N/A"
        )

        lat = card["latency"]
        free_mean = lat["free_sql_ms"]["mean"]
        cube_mean = lat["cube_ms"]["mean"]
        self.stdout.write(
            f"Latency (free):   {free_mean:.0f} ms (n={lat['free_sql_ms']['samples']})"
            if free_mean is not None
            else "Latency (free):   N/A"
        )
        self.stdout.write(
            f"Latency (Cube):   {cube_mean:.0f} ms (n={lat['cube_ms']['samples']})"
            if cube_mean is not None
            else "Latency (Cube):   N/A"
        )

        ans = card["cube_answerable"]
        self.stdout.write(f"Cube answerable:  {ans['count']}/{ans['total_questions']} questions")

        pa = card["preagg_speedup"]
        if pa["speedup_x"] is not None:
            self.stdout.write(
                f"Pre-agg speedup:  {pa['speedup_x']:.2f}x  "
                f"(with={pa['with_preagg_mean_ms']:.0f} ms  "
                f"without={pa['without_preagg_mean_ms']:.0f} ms)"
            )
        else:
            self.stdout.write("Pre-agg speedup:  N/A (need runs with and without --preagg)")

        self.stdout.write("=" * 60 + "\n")
