"""Embed PostgreSQL text in Cube YAML without changing its SQL meaning."""

from collections.abc import Collection

from sqlglot.dialects.postgres import Postgres
from sqlglot.tokens import TokenType


def embed_cube_sql(sql: str, *, references: Collection[str] = ()) -> str:
    """Escape SQL text, interpolating only explicitly trusted, unquoted references.

    This is an output boundary, not SQL validation. Stored SQL and PostgreSQL
    probes must remain unescaped. Cube runs Jinja before its f-string compiler,
    so doubling braces is unsafe; Unicode escapes survive both passes.
    """
    if not references:
        return _escape_text(sql)
    tokens = Postgres().tokenize(sql)
    parts: list[str] = []
    position = 0
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.token_type == TokenType.L_BRACE:
            end = index + 1
            while end < len(tokens) and tokens[end].token_type != TokenType.R_BRACE:
                end += 1
            reference = sql[token.end + 1 : tokens[end].start].strip() if end < len(tokens) else ""
            if reference not in references:
                raise ValueError(f"Unknown Cube SQL reference: {{{reference}}}")
            parts.extend((_escape_text(sql[position : token.start]), "{" + reference + "}"))
            position = tokens[end].end + 1
            index = end
        elif token.token_type == TokenType.R_BRACE:
            raise ValueError("Unmatched closing brace in Cube SQL reference.")
        index += 1
    parts.append(_escape_text(sql[position:]))
    return "".join(parts)


def _escape_text(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
        .replace("{", "\\u007b")
        .replace("}", "\\u007d")
    )
