"""Embed PostgreSQL text in Cube YAML without changing its SQL meaning."""

from collections.abc import Collection

from sqlglot.dialects.postgres import Postgres
from sqlglot.errors import TokenError
from sqlglot.tokens import TokenType


class CubeSQLReferenceError(ValueError):
    def __init__(self, reference: str):
        self.reference = reference
        super().__init__(f"Unknown Cube SQL reference: {{{reference}}}")


def embed_cube_sql(sql: str, *, references: Collection[str] = ()) -> str:
    """Escape SQL text, interpolating only explicitly trusted, unquoted references.

    This is an output boundary, not SQL validation. Stored SQL and PostgreSQL
    probes must remain unescaped. Cube runs Jinja before its f-string compiler,
    so doubling braces is unsafe; Unicode escapes survive both passes.
    """
    if not references:
        return _escape_text(sql)
    try:
        tokens = Postgres().tokenize(sql)
    except TokenError as exc:
        raise ValueError("Invalid SQL text at the Cube embedding boundary.") from exc
    parts: list[str] = []
    position = 0
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.token_type == TokenType.L_BRACE:
            end = index + 1
            while end < len(tokens) and tokens[end].token_type != TokenType.R_BRACE:
                end += 1
            if end >= len(tokens):
                raise ValueError("Unterminated Cube SQL reference: missing closing brace.")
            reference = sql[token.end + 1 : tokens[end].start].strip()
            if reference not in references:
                raise CubeSQLReferenceError(reference)
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
        .replace("`", "\\u0060")
        .replace("$", "\\u0024")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
        .replace("{", "\\u007b")
        .replace("}", "\\u007d")
    )
