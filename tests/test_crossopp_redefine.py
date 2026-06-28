"""Unit coverage for the derived-measure redefine path (F1).

Covers the pure per-opp date-operand resolution + the guarded date-diff expression shape —
the logic that lets 'redefine age_days as days between child_dob and visit_date' resolve each
opportunity's real column (including a differently-named DOB column) deterministically.
"""

from apps.transformations.services import crossopp_measure_service as svc


def test_resolve_date_operand_exact_match():
    cols = {"visit_date", "child_dob", "child_weight_visit"}
    assert svc._resolve_date_operand("s", "visit_date", cols) == "visit_date"
    assert svc._resolve_date_operand("s", "child_dob", cols) == "child_dob"


def test_resolve_date_operand_differently_named_dob():
    # the opp's DOB column is named differently — still resolved via the dob/birth fuzzy match
    cols = {"visit_date", "date_of_birth", "child_weight_visit"}
    assert svc._resolve_date_operand("s", "child_dob", cols) == "date_of_birth"
    cols2 = {"visit_date", "dob", "child_weight_birth"}
    assert svc._resolve_date_operand("s", "child_dob", cols2) == "dob"


def test_resolve_date_operand_not_found():
    cols = {"visit_date", "child_weight_visit"}  # no DOB-like column
    assert svc._resolve_date_operand("s", "child_dob", cols) is None


def test_resolve_date_operand_does_not_match_birth_weight():
    # 'child_weight_birth' contains 'birth' but is NOT a date column — must not be picked
    cols = {"visit_date", "child_weight_birth"}
    assert svc._resolve_date_operand("s", "child_dob", cols) is None


def test_guarded_date_expression_is_iso_guarded():
    expr = svc._guarded_date("child_dob")
    assert "child_dob::date" in expr
    assert "child_dob::text" in expr  # guards a non-date value -> NULL, no cast error
    # The guard is embedded in a Cube model SQL string; Cube strips `{...}`, so the regex
    # MUST NOT use {n} brace quantifiers or the date guard silently matches nothing.
    assert "{" not in expr and "}" not in expr
    assert "[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]" in expr
