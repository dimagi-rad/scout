"""Redaction of legacy SQL examples from knowledge text bound for the system prompt."""

import re

SQL_FENCE_RE = re.compile(r"```sql\b.*?```", re.IGNORECASE | re.DOTALL)
SQL_STATEMENT_LINE_RE = re.compile(
    r"(?im)^\s*(SELECT|WITH|INSERT|UPDATE|DELETE|CREATE|DROP|ALTER)\b.*$"
)
SQL_REDACTION = "[SQL example removed; use semantic_query members instead.]"


def sanitize_prompt_content(value: str) -> str:
    """Remove legacy SQL snippets before knowledge is injected into prompts."""
    value = SQL_FENCE_RE.sub(SQL_REDACTION, value)
    return SQL_STATEMENT_LINE_RE.sub(SQL_REDACTION, value)
