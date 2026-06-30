# Understanding results

When you ask a question, the agent responds with a combination of text, data, and sometimes artifacts. This page explains what to expect.

## Text responses

The agent provides natural language explanations alongside query results. These typically include:

- A summary of what the query found.
- The SQL that was executed, so you can verify the logic.
- Caveats or notes about the data.

## Data tables

Query results are formatted as tables in the chat. Tables include column headers and rows of data. Large result sets are truncated to the project's configured row limit (default: 500 rows).

## Artifacts

When the agent creates a visualization or interactive component, it appears as an artifact. Artifacts render in a separate panel and can be:

- **Charts** (Plotly) -- bar charts, line charts, scatter plots, etc.
- **Dashboards** (React) -- interactive components with multiple views.
- **Documents** (Markdown or HTML) -- formatted reports.
- **Graphics** (SVG) -- static diagrams and illustrations.

See [Artifacts](artifacts.md) for details on working with artifacts.

## Error messages

If something goes wrong, the agent will explain the error:

- **Semantic query validation errors** -- the request referenced an unknown dataset/member or unsupported filter.
- **Execution errors** -- the semantic query ran but failed. The agent will typically retry with corrections automatically.
- **Timeout errors** -- the query exceeded the configured timeout. Try simplifying the query or adding filters to reduce the data scanned.
- **Rate limit errors** -- you've exceeded the per-user or per-project query quota. Wait a moment and try again.

## Self-correction

When a query fails due to a correctable error (like a wrong column name or table reference), the agent automatically retries with corrections, up to three times. You'll see the agent explain what went wrong and what it changed.

Over time, the agent saves these corrections as learnings and applies them to future queries, so the same mistakes don't happen again.
