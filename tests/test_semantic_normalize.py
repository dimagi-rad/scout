"""The stray-quote MEASURE() normalization that prevents a blank artifact chart."""

from mcp_server.services.semantic import _normalize_semantic_sql


def test_unwraps_quoted_measure_lowercase():
    sql = 'SELECT age_week, "measure(kmc_cross_opp.avg_visit_weight)" AS avg FROM kmc_cross_opp'
    out = _normalize_semantic_sql(sql)
    assert 'MEASURE(kmc_cross_opp.avg_visit_weight)' in out
    assert '"measure(' not in out


def test_unwraps_quoted_dimension_and_preserves_good_sql():
    sql = 'SELECT "dimension(c.age_week)", MEASURE(c.children) FROM c'
    out = _normalize_semantic_sql(sql)
    assert 'DIMENSION(c.age_week)' in out
    assert 'MEASURE(c.children)' in out  # already-correct call untouched


def test_leaves_unquoted_measure_alone():
    sql = 'SELECT MEASURE(c.avg_visit_weight) FROM c'
    assert _normalize_semantic_sql(sql) == sql
