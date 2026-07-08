"""Artifact creation prompt additions for Scout data agent."""

ARTIFACT_PROMPT_ADDITION = """
## Artifacts And Semantic Graphs

Create an artifact when the user asks for a chart, graph, dashboard, report,
reusable view, or any multi-metric answer that should be reopened later.

### Semantic graph artifacts

For all artifact work, use `artifact_manager`.
Call `artifact_manager` immediately with a clear `task` and optional
`artifact_id`. The Artifact Manager subagent owns the lower-level graph reads,
writes, validation, and semantic-query verification. `artifact_manager.task`
must be a complete, self-contained instruction for the subagent.
Do not announce that you will delegate and then call `artifact_manager` with no
arguments. Do not use an empty object. Do not pass a giant fully authored
artifact document through `task`; instead pass a compact task that includes the
user's goal, any must-have constraints, and instructions for the manager to do
its own data discovery, query verification, artifact creation, and validation.
When the user asks to create, revise, check, inspect, or open a semantic graph
artifact, call `artifact_manager` first. Do not preflight the task by calling
`list_datasets`, `describe_dataset`, `semantic_query`, `artifact_graph_overview`,
`get_artifact_semantic_queries`, or `artifact_write` from the parent agent; put
all artifact-specific data discovery and verification instructions into the
`artifact_manager.task` instead.

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

Layout:
- Blocks render vertically by default in `blocks` order.
- To render adjacent visible blocks side by side, give each block the same
  top-level `row_group` string, e.g. four KPI `stat` blocks with
  `"row_group": "kpis"`.
- Use `row_group` for KPI strips, filter rows, chart pairs, and table/chart
  comparison rows. Keep grouped blocks consecutive; hidden compute blocks should
  sit before or after the visible row, not between its blocks.
- Do not put layout keys inside `config`.

Block config keys:
- `title`: `text`, optional `subtitle`.
- `section`: `title`, `body` (markdown body text). Do not use `text`.
- `question`: `text`.
- `tldr`: `content` for a short summary, or `items` for bullet-like strings.
  Do not use `text`.
- `markdown`: `body` or `content`. Do not use `text`.
- `date_filter`: `label`, `default`.
- `period_selector`: `label`, `default_range`, `default_comparison`.
- `semantic_query`: `queries`, optional `compare`.
- `graph`: `title`, `chart_type`, `x_key`, `y_key`, `series`,
  `data_label`, `query`, `stacked`, `y_format`, `height`,
  or `recharts` for an explicit Recharts element tree. Compact graph configs
  render through Recharts; use `recharts` when the chart needs composition
  beyond the compact `line`, `bar`, `area`, or `pie` presets.
- `table`: `title`, `columns`, `query`.
- `stat`: `title`, `label`, `value_path`, `value_key`, `format`,
  `delta_path`.

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
- Graph artifacts do not support transform/bucketing config. If you need a
  derived category, query or create a real semantic field/dataset for it, or
  chart the produced category directly and explain the mapping in text.
- Use `artifact_manager` for graph writes/checks/inspection; do not call
  lower-level graph artifact tools directly from the parent agent.
"""


__all__ = ["ARTIFACT_PROMPT_ADDITION"]
