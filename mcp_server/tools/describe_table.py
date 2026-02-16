"""MCP tool for describing table schema from a Scout project's data dictionary."""

from __future__ import annotations

import logging

from mcp.server.fastmcp import FastMCP

from apps.projects.models import Project

logger = logging.getLogger(__name__)


def register_describe_table(mcp: FastMCP) -> None:
    """Register the describe_table tool on the MCP server."""

    @mcp.tool()
    def describe_table(project_slug: str, table_name: str) -> str:
        """
        Get detailed column information for a table in a Scout project.

        Returns column names, types, nullability, primary/foreign keys,
        indexes, and sample values from the project's data dictionary.

        Args:
            project_slug: The slug of the Scout project.
            table_name: The table to describe (case-sensitive).

        Returns:
            Formatted markdown with the table's schema, or an error message.
        """
        try:
            project = Project.objects.get(slug=project_slug)
        except Project.DoesNotExist:
            return f"Project '{project_slug}' not found."

        dd = project.data_dictionary
        if not dd:
            return (
                "No data dictionary is available for this project. "
                "Ask an administrator to generate the data dictionary."
            )

        tables = dd.get("tables", {})

        if table_name not in tables:
            available = sorted(tables.keys())

            # Case-insensitive match
            matches = [t for t in available if t.lower() == table_name.lower()]
            if matches:
                return f"Table '{table_name}' not found. Did you mean '{matches[0]}'?"

            # Partial match
            partials = [t for t in available if table_name.lower() in t.lower()]
            suggestion = ""
            if partials:
                suggestion = "\n\nDid you mean one of these?\n- " + "\n- ".join(partials[:5])

            if len(available) <= 20:
                return (
                    f"Table '{table_name}' not found.\n\n"
                    f"Available tables:\n- " + "\n- ".join(available) + suggestion
                )
            return (
                f"Table '{table_name}' not found.\n\n"
                f"{len(available)} tables available. "
                f"First 20: {', '.join(available[:20])}..." + suggestion
            )

        tinfo = tables[table_name]
        lines: list[str] = []

        lines.append(f"## {table_name}")
        lines.append("")

        if tinfo.get("comment"):
            lines.append(f"**Description:** {tinfo['comment']}")
            lines.append("")

        row_count = tinfo.get("row_count", 0)
        lines.append(f"**Approximate rows:** {row_count:,}" if row_count else "**Approximate rows:** Unknown")
        lines.append("")

        columns = tinfo.get("columns", [])
        if columns:
            lines.append("### Columns")
            lines.append("")
            lines.append("| Column | Type | Nullable | PK | Description | Sample Values |")
            lines.append("|--------|------|----------|----:|-------------|---------------|")

            for col in columns:
                name = col.get("name", "")
                col_type = col.get("type", "unknown")
                nullable = "Yes" if col.get("nullable") else ""
                pk = "*" if col.get("is_primary_key") else ""
                comment = col.get("comment", "") or ""
                samples = col.get("sample_values")

                if samples:
                    sample_strs = []
                    for s in samples[:3]:
                        s_str = str(s) if s is not None else "NULL"
                        if len(s_str) > 20:
                            s_str = s_str[:17] + "..."
                        sample_strs.append(f"`{s_str}`")
                    sample_str = ", ".join(sample_strs)
                else:
                    sample_str = ""

                if len(comment) > 40:
                    comment = comment[:37] + "..."

                lines.append(f"| {name} | {col_type} | {nullable} | {pk} | {comment} | {sample_str} |")
            lines.append("")

        # Foreign keys
        fks = tinfo.get("foreign_keys", [])
        if fks:
            lines.append("### Relationships (Foreign Keys)")
            lines.append("")
            for fk in fks:
                col = fk.get("column", "")
                ref_table = fk.get("references_table", "")
                ref_col = fk.get("references_column", "")
                lines.append(f"- `{col}` -> `{ref_table}.{ref_col}`")
            lines.append("")

        # Indexes
        indexes = tinfo.get("indexes", [])
        if indexes:
            lines.append("### Indexes")
            lines.append("")
            for idx in indexes:
                idx_name = idx.get("name", "")
                idx_cols = idx.get("columns", [])
                is_unique = idx.get("unique", False)
                unique_str = " (unique)" if is_unique else ""
                cols_str = ", ".join(f"`{c}`" for c in idx_cols)
                lines.append(f"- **{idx_name}**: {cols_str}{unique_str}")
            lines.append("")

        return "\n".join(lines).rstrip()
