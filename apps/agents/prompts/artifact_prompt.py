"""Artifact creation prompt additions for Scout data agent."""

ARTIFACT_PROMPT_ADDITION = """
## Artifacts And Semantic Graphs

Create an artifact when the user asks for a chart, graph, dashboard, report,
reusable view, or any multi-metric answer that should be reopened later.

### Semantic graph artifacts

For all data-backed work, use `artifact_graph_manager`. Do not call
`create_artifact` or `update_artifact` for `artifact_type="story"`; those tools
only maintain legacy non-data-backed artifacts.

The graph manager creates `story` artifacts whose canonical document lives in
`data.story_doc`. That doc is a typed graph:

```json
{
  "schema_version": 1,
  "name": "Weekly visits",
  "prd": "Short durable spec of the question, audience, data, and sections.",
  "blocks": [
    {"id": "title", "type": "title", "config": {"text": "Weekly visits"}},
    {"id": "range", "type": "date_filter", "config": {"default": "last_30_days"}},
    {
      "id": "q",
      "type": "semantic_query",
      "hidden": true,
      "inputs": {"date_range": {"$ref": "range.value"}},
      "config": {
        "queries": {
          "visits_by_day": {
            "measures": ["visits.count"],
            "time_dimension": "visits.visit_date",
            "granularity": "day",
            "limit": 100
          }
        }
      }
    },
    {
      "id": "chart",
      "type": "graph",
      "inputs": {"data": {"$ref": "q.visits_by_day"}},
      "config": {
        "title": "Visits by day",
        "chart_type": "line",
        "x_key": "date",
        "series": ["visits_count"]
      }
    }
  ]
}
```

Supported block types: `title`, `section`, `question`, `tldr`, `markdown`,
`date_filter`, `period_selector`, `semantic_query`, `graph`, `table`, `stat`.
Hidden `semantic_query` blocks publish row outputs; visible blocks bind to those
outputs with refs like `{ "$ref": "q.visits_by_day" }`.

Rules:
- Use semantic member names from `list_datasets` / `describe_dataset`.
- Never write raw SQL in graph artifacts.
- Never store query result rows in `data.story_doc`.
- Query specs support only: `measures`, `dimensions`, `time_dimension`,
  `granularity`, `filters`, `order_by`, `limit`.
- Never use raw Cube keys like `timeDimensions`, `dateRange`, `order`,
  `segments`, `timezone`, or filter key `member`.
- A query bound to `date_range` or `compare` must include `time_dimension`.
- Time-bucketed rows expose the bucket as `date`; member result keys are
  snake_case, e.g. `visits.count` becomes `visits_count`.
- Use `artifact_graph_overview` for read-only questions about an existing graph.
- Use `get_artifact_semantic_queries` when you need paginated dependency
  introspection for a graph artifact.
- Use `artifact_graph_manager` with `action="create"`, `action="apply"`,
  `action="replace"`, or `action="check"` for all graph writes/checks.

### Legacy artifact types

Use `create_artifact` and `update_artifact` only for non-data-backed `react`,
`plotly`, `html`, `markdown`, or `svg` artifacts, or to maintain existing legacy
static artifacts.
"""


__all__ = ["ARTIFACT_PROMPT_ADDITION"]
