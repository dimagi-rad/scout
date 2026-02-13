"""
Evaluation runner for golden query testing.

This module provides the EvalRunner class that runs golden queries through
the agent and compares results against expected values.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from django.utils import timezone

if TYPE_CHECKING:
    from apps.knowledge.models import EvalRun, GoldenQuery
    from apps.projects.models import Project
    from apps.users.models import User

logger = logging.getLogger(__name__)


@dataclass
class QueryResult:
    """Result of running a single golden query."""

    golden_query_id: str
    question: str
    passed: bool
    expected: Any
    actual: Any
    error: str | None = None
    latency_ms: int = 0
    sql_executed: str | None = None
    comparison_details: dict = field(default_factory=dict)


class ComparisonError(Exception):
    """Raised when result comparison fails."""

    pass


class EvalRunner:
    """
    Runs golden queries through the agent and evaluates results.

    The EvalRunner:
    1. Fetches golden queries for a project (with optional filters)
    2. Runs each query through the agent
    3. Compares results against expected values
    4. Records pass/fail status and accuracy
    5. Saves an EvalRun record with detailed results
    """

    def __init__(
        self,
        project: Project,
        user: User | None = None,
        tags: list[str] | None = None,
        difficulty: str | None = None,
    ):
        """
        Initialize the EvalRunner.

        Args:
            project: The project to evaluate
            user: Optional user running the eval
            tags: Optional list of tags to filter golden queries
            difficulty: Optional difficulty level to filter (easy, medium, hard)
        """
        self.project = project
        self.user = user
        self.tags = tags or []
        self.difficulty = difficulty
        self._results: list[QueryResult] = []

    def _get_golden_queries(self) -> list[GoldenQuery]:
        """
        Get golden queries matching the filter criteria.

        Returns:
            List of GoldenQuery instances
        """
        from apps.knowledge.models import GoldenQuery

        queryset = GoldenQuery.objects.filter(project=self.project)

        if self.difficulty:
            queryset = queryset.filter(difficulty=self.difficulty)

        if self.tags:
            # Filter queries that have at least one of the specified tags
            # JSONField contains requires special handling
            from django.db.models import Q

            tag_filter = Q()
            for tag in self.tags:
                tag_filter |= Q(tags__contains=[tag])
            queryset = queryset.filter(tag_filter)

        return list(queryset)

    def _run_query(self, golden_query: GoldenQuery) -> QueryResult:
        """
        Run a single golden query through the agent.

        Args:
            golden_query: The golden query to run

        Returns:
            QueryResult with the outcome
        """
        from langgraph.checkpoint.memory import MemorySaver

        from apps.agents.graph.base import build_agent_graph

        start_time = time.time()
        error = None
        actual_result = None
        sql_executed = None

        try:
            # Build agent graph for this project
            checkpointer = MemorySaver()
            graph = build_agent_graph(
                project=self.project,
                user=self.user,
                checkpointer=checkpointer,
            )

            # Run the query through the agent
            config = {"configurable": {"thread_id": f"eval_{golden_query.id}"}}
            result = graph.invoke(
                {"messages": [("human", golden_query.question)]},
                config=config,
            )

            # Extract the result from the agent's response
            # Look for SQL tool results in the message history
            actual_result = self._extract_result(result)
            sql_executed = self._extract_sql(result)

        except Exception as e:
            logger.exception(
                "Error running golden query %s: %s", golden_query.id, str(e)
            )
            error = str(e)
            actual_result = None

        latency_ms = int((time.time() - start_time) * 1000)

        # Compare results
        passed = False
        comparison_details = {}

        if error is None and actual_result is not None:
            try:
                passed, comparison_details = self._compare_results(
                    expected=golden_query.expected_result,
                    actual=actual_result,
                    mode=golden_query.comparison_mode,
                    tolerance=golden_query.tolerance,
                )
            except ComparisonError as e:
                error = f"Comparison error: {e}"

        return QueryResult(
            golden_query_id=str(golden_query.id),
            question=golden_query.question,
            passed=passed,
            expected=golden_query.expected_result,
            actual=actual_result,
            error=error,
            latency_ms=latency_ms,
            sql_executed=sql_executed,
            comparison_details=comparison_details,
        )

    def _extract_result(self, agent_result: dict) -> Any:
        """
        Extract the query result from the agent's response.

        Args:
            agent_result: The result from graph.invoke()

        Returns:
            The extracted result data, or None if not found
        """
        messages = agent_result.get("messages", [])

        # Look for tool messages with SQL results
        for msg in reversed(messages):
            if hasattr(msg, "type") and msg.type == "tool":
                # Parse the tool result
                content = msg.content if hasattr(msg, "content") else str(msg)
                if isinstance(content, str):
                    try:
                        import json

                        data = json.loads(content)
                        if isinstance(data, dict) and "rows" in data:
                            return data
                    except (json.JSONDecodeError, TypeError):
                        pass

        # If no tool result, return None
        return None

    def _extract_sql(self, agent_result: dict) -> str | None:
        """
        Extract the executed SQL from the agent's response.

        Args:
            agent_result: The result from graph.invoke()

        Returns:
            The SQL that was executed, or None
        """
        messages = agent_result.get("messages", [])

        for msg in reversed(messages):
            if hasattr(msg, "type") and msg.type == "tool":
                content = msg.content if hasattr(msg, "content") else str(msg)
                if isinstance(content, str):
                    try:
                        import json

                        data = json.loads(content)
                        if isinstance(data, dict) and "sql_executed" in data:
                            return data["sql_executed"]
                    except (json.JSONDecodeError, TypeError):
                        pass

        return None

    def _compare_results(
        self,
        expected: Any,
        actual: Any,
        mode: str,
        tolerance: float,
    ) -> tuple[bool, dict]:
        """
        Compare expected and actual results.

        Args:
            expected: Expected result
            actual: Actual result from agent
            mode: Comparison mode (exact, approximate, row_count, contains, structure)
            tolerance: Tolerance for approximate comparison

        Returns:
            Tuple of (passed, details_dict)
        """
        if mode == "exact":
            return self._compare_exact(expected, actual)
        elif mode == "approximate":
            return self._compare_approximate(expected, actual, tolerance)
        elif mode == "row_count":
            return self._compare_row_count(expected, actual)
        elif mode == "contains":
            return self._compare_contains(expected, actual)
        elif mode == "structure":
            return self._compare_structure(expected, actual)
        else:
            raise ComparisonError(f"Unknown comparison mode: {mode}")

    def _compare_exact(self, expected: Any, actual: Any) -> tuple[bool, dict]:
        """Exact match comparison."""
        # Normalize for comparison
        expected_normalized = self._normalize_for_comparison(expected)
        actual_normalized = self._normalize_for_comparison(actual)

        passed = expected_normalized == actual_normalized
        return passed, {
            "mode": "exact",
            "expected_normalized": expected_normalized,
            "actual_normalized": actual_normalized,
        }

    def _compare_approximate(
        self, expected: Any, actual: Any, tolerance: float
    ) -> tuple[bool, dict]:
        """Approximate comparison with tolerance for numeric values."""
        details = {"mode": "approximate", "tolerance": tolerance, "mismatches": []}

        expected_data = self._extract_data(expected)
        actual_data = self._extract_data(actual)

        if len(expected_data) != len(actual_data):
            details["mismatches"].append(
                f"Row count mismatch: expected {len(expected_data)}, got {len(actual_data)}"
            )
            return False, details

        for i, (exp_row, act_row) in enumerate(zip(expected_data, actual_data)):
            if not isinstance(exp_row, (list, tuple)) or not isinstance(
                act_row, (list, tuple)
            ):
                if not self._values_match(exp_row, act_row, tolerance):
                    details["mismatches"].append(
                        f"Row {i}: expected {exp_row}, got {act_row}"
                    )
                continue

            if len(exp_row) != len(act_row):
                details["mismatches"].append(
                    f"Row {i} column count mismatch: expected {len(exp_row)}, got {len(act_row)}"
                )
                continue

            for j, (exp_val, act_val) in enumerate(zip(exp_row, act_row)):
                if not self._values_match(exp_val, act_val, tolerance):
                    details["mismatches"].append(
                        f"Row {i}, Col {j}: expected {exp_val}, got {act_val}"
                    )

        passed = len(details["mismatches"]) == 0
        return passed, details

    def _compare_row_count(self, expected: Any, actual: Any) -> tuple[bool, dict]:
        """Compare only the number of rows."""
        expected_count = self._get_row_count(expected)
        actual_count = self._get_row_count(actual)

        passed = expected_count == actual_count
        return passed, {
            "mode": "row_count",
            "expected_count": expected_count,
            "actual_count": actual_count,
        }

    def _compare_contains(self, expected: Any, actual: Any) -> tuple[bool, dict]:
        """Check if actual result contains all expected values."""
        expected_data = self._extract_data(expected)
        actual_data = self._extract_data(actual)

        # Convert to sets of tuples for comparison
        expected_set = set(tuple(row) if isinstance(row, list) else (row,) for row in expected_data)
        actual_set = set(tuple(row) if isinstance(row, list) else (row,) for row in actual_data)

        missing = expected_set - actual_set
        passed = len(missing) == 0

        return passed, {
            "mode": "contains",
            "expected_count": len(expected_set),
            "actual_count": len(actual_set),
            "missing_count": len(missing),
            "missing_sample": list(missing)[:5] if missing else [],
        }

    def _compare_structure(self, expected: Any, actual: Any) -> tuple[bool, dict]:
        """Compare column structure (names and types)."""
        expected_cols = self._get_columns(expected)
        actual_cols = self._get_columns(actual)

        # Compare column names (case-insensitive)
        expected_names = [c.lower() for c in expected_cols]
        actual_names = [c.lower() for c in actual_cols]

        passed = expected_names == actual_names
        return passed, {
            "mode": "structure",
            "expected_columns": expected_cols,
            "actual_columns": actual_cols,
            "match": passed,
        }

    def _normalize_for_comparison(self, data: Any) -> Any:
        """Normalize data for exact comparison."""
        if isinstance(data, dict):
            if "rows" in data:
                return sorted([tuple(row) for row in data["rows"]])
            return data
        if isinstance(data, list):
            return sorted([tuple(row) if isinstance(row, list) else row for row in data])
        return data

    def _extract_data(self, data: Any) -> list:
        """Extract row data from various formats."""
        if isinstance(data, dict) and "rows" in data:
            return data["rows"]
        if isinstance(data, list):
            return data
        return [data]

    def _get_row_count(self, data: Any) -> int:
        """Get row count from data."""
        if isinstance(data, dict):
            if "row_count" in data:
                return data["row_count"]
            if "rows" in data:
                return len(data["rows"])
        if isinstance(data, list):
            return len(data)
        if isinstance(data, int):
            return data
        return 0

    def _get_columns(self, data: Any) -> list[str]:
        """Get column names from data."""
        if isinstance(data, dict) and "columns" in data:
            return data["columns"]
        return []

    def _values_match(self, expected: Any, actual: Any, tolerance: float) -> bool:
        """Check if two values match within tolerance."""
        if expected == actual:
            return True

        # Numeric comparison with tolerance
        try:
            exp_num = float(expected)
            act_num = float(actual)
            if exp_num == 0:
                return abs(act_num) <= tolerance
            return abs(exp_num - act_num) / abs(exp_num) <= tolerance
        except (TypeError, ValueError):
            pass

        # String comparison (case-insensitive for certain types)
        if isinstance(expected, str) and isinstance(actual, str):
            return expected.lower() == actual.lower()

        return False

    def run(self) -> EvalRun:
        """
        Run the evaluation and return the EvalRun record.

        Returns:
            EvalRun instance with results
        """
        from apps.knowledge.models import EvalRun

        golden_queries = self._get_golden_queries()

        if not golden_queries:
            logger.warning(
                "No golden queries found for project %s with filters: tags=%s, difficulty=%s",
                self.project.slug,
                self.tags,
                self.difficulty,
            )

        # Run each query
        self._results = []
        for gq in golden_queries:
            logger.info("Running golden query: %s", gq.question[:80])
            result = self._run_query(gq)
            self._results.append(result)

        # Calculate statistics
        total = len(self._results)
        passed = sum(1 for r in self._results if r.passed)
        failed = sum(1 for r in self._results if not r.passed and r.error is None)
        errored = sum(1 for r in self._results if r.error is not None)
        accuracy = passed / total if total > 0 else 0.0

        # Build results list for storage
        results_data = [
            {
                "golden_query_id": r.golden_query_id,
                "question": r.question,
                "passed": r.passed,
                "expected": r.expected,
                "actual": r.actual,
                "error": r.error,
                "latency_ms": r.latency_ms,
                "sql_executed": r.sql_executed,
                "comparison_details": r.comparison_details,
            }
            for r in self._results
        ]

        # Create EvalRun record
        eval_run = EvalRun.objects.create(
            project=self.project,
            model_used="claude-sonnet-4-20250514",  # From settings
            knowledge_snapshot=self._get_knowledge_snapshot(),
            total_queries=total,
            passed=passed,
            failed=failed,
            errored=errored,
            accuracy=accuracy,
            results=results_data,
            completed_at=timezone.now(),
            triggered_by=self.user,
        )

        logger.info(
            "Eval completed: %d/%d passed (%.1f%% accuracy)",
            passed,
            total,
            accuracy * 100,
        )

        return eval_run

    def _get_knowledge_snapshot(self) -> dict:
        """Get a snapshot of the current knowledge state."""
        from apps.knowledge.models import (
            AgentLearning,
            KnowledgeEntry,
            TableKnowledge,
        )

        return {
            "table_knowledge_count": TableKnowledge.objects.filter(
                project=self.project
            ).count(),
            "knowledge_entry_count": KnowledgeEntry.objects.filter(
                project=self.project
            ).count(),
            "agent_learning_count": AgentLearning.objects.filter(
                project=self.project, is_active=True
            ).count(),
            "filters_used": {
                "tags": self.tags,
                "difficulty": self.difficulty,
            },
        }

    @property
    def results(self) -> list[QueryResult]:
        """Get the list of query results."""
        return self._results


__all__ = ["EvalRunner", "QueryResult", "ComparisonError"]
