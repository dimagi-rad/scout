"""
Tests for apps.evals models: GoldenQuery and EvalRun.

Verifies that both models can be created, saved, and retrieved with all key
fields intact, including JSONField round-trips and semantic_equivalence choices.
"""

import pytest

from apps.evals.models import EvalRun, GoldenQuery


@pytest.mark.django_db(transaction=True)
def test_golden_query_create_and_retrieve(workspace):
    """GoldenQuery can be created with all fields and retrieved by PK."""
    gq = GoldenQuery.objects.create(
        workspace=workspace,
        title="Total revenue by month",
        question="What is the total revenue grouped by month for the last 12 months?",
        reference_sql="SELECT date_trunc('month', created_at), SUM(amount) FROM orders GROUP BY 1",
        expected_summary="12 rows, one per month, with a SUM(amount) column in cents.",
        source="user_provided",
    )

    fetched = GoldenQuery.objects.get(pk=gq.pk)
    assert fetched.title == "Total revenue by month"
    assert fetched.question == "What is the total revenue grouped by month for the last 12 months?"
    assert "SUM(amount)" in fetched.reference_sql
    assert "12 rows" in fetched.expected_summary
    assert fetched.source == "user_provided"
    assert fetched.workspace == workspace
    assert fetched.created_at is not None
    assert fetched.updated_at is not None


@pytest.mark.django_db(transaction=True)
def test_golden_query_optional_fields(workspace):
    """GoldenQuery can be saved with only required fields."""
    gq = GoldenQuery.objects.create(
        workspace=workspace,
        title="Minimal query",
        question="How many users signed up today?",
    )
    fetched = GoldenQuery.objects.get(pk=gq.pk)
    assert fetched.reference_sql == ""
    assert fetched.expected_summary == ""
    assert fetched.source == ""


@pytest.mark.django_db(transaction=True)
def test_golden_query_no_workspace():
    """GoldenQuery can be created without a workspace (null=True)."""
    gq = GoldenQuery.objects.create(
        title="Global query",
        question="Total row count across all tables?",
    )
    fetched = GoldenQuery.objects.get(pk=gq.pk)
    assert fetched.workspace is None


@pytest.mark.django_db(transaction=True)
def test_eval_run_create_and_retrieve(workspace):
    """EvalRun can be created with all fields and retrieved with correct values."""
    gq = GoldenQuery.objects.create(
        workspace=workspace,
        title="Active users",
        question="How many active users are there?",
    )

    free_result = [{"count": 42}]
    cube_result = [{"Users.count": "42"}]

    run = EvalRun.objects.create(
        workspace=workspace,
        golden_query=gq,
        free_sql="SELECT COUNT(*) AS count FROM users WHERE active = true",
        free_sql_result=free_result,
        free_sql_ms=120,
        cube_query='{"measures": ["Users.count"], "filters": [{"member": "Users.active", "operator": "equals", "values": ["true"]}]}',
        cube_result=cube_result,
        cube_ms=45,
        result_match=True,
        match_confidence=0.95,
        semantic_equivalence="exact",
        used_preaggregation=True,
    )

    fetched = EvalRun.objects.get(pk=run.pk)
    assert fetched.golden_query == gq
    assert fetched.workspace == workspace
    assert fetched.free_sql_ms == 120
    assert fetched.cube_ms == 45
    assert fetched.result_match is True
    assert fetched.match_confidence == pytest.approx(0.95)
    assert fetched.semantic_equivalence == "exact"
    assert fetched.used_preaggregation is True
    assert fetched.created_at is not None


@pytest.mark.django_db(transaction=True)
def test_eval_run_json_field_round_trip(workspace):
    """JSONField values for free_sql_result and cube_result survive DB round-trip."""
    gq = GoldenQuery.objects.create(
        workspace=workspace,
        title="JSON test",
        question="Return something complex.",
    )
    complex_result = [
        {"month": "2025-01", "revenue": 100000, "currency": "USD"},
        {"month": "2025-02", "revenue": 120000, "currency": "USD"},
    ]
    run = EvalRun.objects.create(
        workspace=workspace,
        golden_query=gq,
        free_sql_result=complex_result,
        cube_result={"data": complex_result, "annotation": {"measures": {}}},
    )

    fetched = EvalRun.objects.get(pk=run.pk)
    assert fetched.free_sql_result == complex_result
    assert fetched.cube_result["data"] == complex_result
    assert fetched.cube_result["annotation"] == {"measures": {}}


@pytest.mark.django_db(transaction=True)
def test_eval_run_semantic_equivalence_choices(workspace):
    """semantic_equivalence accepts all defined choices."""
    gq = GoldenQuery.objects.create(
        workspace=workspace,
        title="Choices test",
        question="Anything.",
    )
    for choice_value in ("exact", "approximate", "failed"):
        run = EvalRun.objects.create(
            workspace=workspace,
            golden_query=gq,
            semantic_equivalence=choice_value,
        )
        fetched = EvalRun.objects.get(pk=run.pk)
        assert fetched.semantic_equivalence == choice_value


@pytest.mark.django_db(transaction=True)
def test_eval_run_null_fields(workspace):
    """EvalRun can be created with nullable/blank fields left unset."""
    gq = GoldenQuery.objects.create(
        workspace=workspace,
        title="Sparse run",
        question="What?",
    )
    run = EvalRun.objects.create(workspace=workspace, golden_query=gq)
    fetched = EvalRun.objects.get(pk=run.pk)
    assert fetched.free_sql_result is None
    assert fetched.cube_result is None
    assert fetched.free_sql_ms is None
    assert fetched.cube_ms is None
    assert fetched.result_match is None
    assert fetched.match_confidence is None
    assert fetched.semantic_equivalence == ""
    assert fetched.used_preaggregation is False


@pytest.mark.django_db(transaction=True)
def test_eval_run_related_name(workspace):
    """golden_query.runs reverse relation returns linked EvalRun instances."""
    gq = GoldenQuery.objects.create(
        workspace=workspace,
        title="Multi-run query",
        question="Count something.",
    )
    EvalRun.objects.create(workspace=workspace, golden_query=gq, semantic_equivalence="exact")
    EvalRun.objects.create(workspace=workspace, golden_query=gq, semantic_equivalence="approximate")

    runs = list(gq.runs.all())
    assert len(runs) == 2
    equivalences = {r.semantic_equivalence for r in runs}
    assert equivalences == {"exact", "approximate"}


@pytest.mark.django_db(transaction=True)
def test_golden_query_ordering(workspace):
    """GoldenQuery default ordering is alphabetical by title."""
    GoldenQuery.objects.create(workspace=workspace, title="Zebra query", question="Z?")
    GoldenQuery.objects.create(workspace=workspace, title="Alpha query", question="A?")
    GoldenQuery.objects.create(workspace=workspace, title="Mango query", question="M?")

    titles = list(GoldenQuery.objects.filter(workspace=workspace).values_list("title", flat=True))
    assert titles == ["Alpha query", "Mango query", "Zebra query"]
