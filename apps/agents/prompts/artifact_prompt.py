"""Artifact creation prompt additions for Scout data agent."""

ARTIFACT_PROMPT_ADDITION = """
## Artifacts And Semantic Graphs

Create an artifact when the user asks for a chart, graph, dashboard, report,
reusable view, or any multi-metric answer that should be reopened later.

### Semantic graph artifacts

For all artifact work, use `artifact_manager`.
All charts render with Recharts. Never create or request a Plotly artifact or
Plotly specification; Plotly is not part of Scout's artifact runtime.
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
  "prd": "One or two short user-facing sentences about the question and data scope.",
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
- `question`: `text`, optional `context`.
- `tldr`: optional `title`, plus `content` for a short summary or `items` for
  takeaway strings. Do not use `text`.
- `markdown`: `body` or `content`. Do not use `text`.
- `date_filter`: `label`, `default`.
- `period_selector`: `label`, `default_range`, `default_comparison`.
- `semantic_query`: `queries`, optional `compare`.
- `graph`: `title`, `chart_type`, `x_key`, `y_key`, `series`,
  `subtitle`, `data_label`, `query`, `stacked`, `y_format`, `height`,
  `x_label`, `y_label`, `style`,
  or `recharts` for an explicit Recharts element tree. Compact graph configs
  render through Recharts; use `recharts` when the chart needs composition
  beyond the compact `line`, `bar`, `area`, `pie`, or `donut` presets.
- `table`: `title`, `columns`, `query`.
- `stat`: `title`, `label`, `value_path`, `value_key`, `format`,
  `delta_path`, optional `prefix`, `suffix`, and `comparison`.

Visualization grammar:
- Choose the chart from the analytical comparison: one headline measure ->
  `stat`; time plus measure -> `line`; categories plus measure -> sorted `bar`
  (use horizontal orientation for long labels); two independently varying
  measures -> explicit Recharts `ScatterChart`; a few part-to-whole categories
  -> `donut` or a 100% stacked bar; detailed or high-cardinality rows -> `table`.
- A compact graph `style` is a bounded object. It may contain:
  `palette` (`categorical`, `status`, `sequential`, `monochrome`), `legend`
  (`auto`, `top`, `bottom`, `none`), `grid` (`horizontal`, `both`, `none`),
  `curve` (`monotone`, `linear`, `step`), `orientation` (`vertical`,
  `horizontal`), and `labels` (`none`, `value`). Prefer quiet horizontal grids,
  at most five category colors, and value labels only when they do not crowd.
- A stat `comparison` may contain `type` (`none`, `absolute`, `percent`),
  `format`, `label`, and `goal` (`higher`, `lower`, `neutral`). Use `goal` only
  when metric meaning establishes whether movement is favorable; otherwise use
  `neutral`. Do not encode good/bad by choosing raw red or green colors.
- Named formats support decimal suffixes, for example `number_0`, `percent_1`,
  `currency_2`, `accounting_0`, and `compact_1`.
- Set `y_format` to match the measure's semantics: counts use `number_0`, money
  uses a currency or accounting format, and rates use a percent format. This
  keeps chart axes and tooltips honest and avoids fractional count ticks.
- `prd` renders in the artifact. Keep it to one or two concise, user-facing
  sentences about the question and data scope. Do not list implementation
  details, block IDs, semantic member names, or section inventories there.
- Use graph subtitles for units, date window, denominator, sample size, or
  synthetic/demo disclosure when that context is needed to read the chart
  honestly. Do not invent a takeaway in the title.

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
