"""Artifact creation prompt additions for Scout data agent."""

from apps.agents.prompts.query_guidance import RECENT_PERIOD_QUERY_GUIDANCE

ARTIFACT_PROMPT_ADDITION = "".join(
    (
        """
## Artifacts And Semantic Graphs

Create an artifact when the user asks for a chart, graph, dashboard, report,
reusable view, or any multi-metric answer that should be reopened later.

### Semantic graph artifacts

Rolling dates are resolved by Scout, not calculated by the model.
For the supported rolling presets use date_filter (today, yesterday, last_7_days,
last_30_days, last_90_days, month_to_date) and bind every affected query's
date_range input to range.value. For comparisons use period_selector, bind
inputs.compare to period.pair, and set config.compare=true on semantic_query.
Last N days includes today plus N-1 preceding calendar days.
Never put preset strings in inDateRange. For standalone semantic_query
calls use date_range={"preset":"last_30_days"}; fixed ranges use
date_range={"start":"YYYY-MM-DD","end":"YYYY-MM-DD"}. Keep empty requested
windows empty rather than silently replacing them with all-time data. The
current clock and the latest available source date are different facts.

For artifact reads, writes, and validation, use `artifact_manager`.
All charts render with Recharts. Never create or request a Plotly artifact or
Plotly specification; Plotly is not part of Scout's artifact runtime.
For questions answerable by existing semantic fields, call `artifact_manager`
immediately with a clear `task` and optional
`artifact_id`. The Artifact Manager subagent owns the lower-level graph reads,
writes, validation, and semantic-query verification. `artifact_manager.task`
must be a complete, self-contained instruction for the subagent.
Do not announce that you will delegate and then call `artifact_manager` with no
arguments. Do not use an empty object. Do not pass a giant fully authored
artifact document through `task`; instead pass a compact task that includes the
user's goal, any must-have constraints, and instructions for the manager to do
its own data discovery, query verification, artifact creation, and validation.
When the user asks to create, revise, check, inspect, or open an artifact over
existing semantic fields, call `artifact_manager` first. Do not preflight the task by calling
`list_datasets`, `describe_dataset`, `semantic_query`, `artifact_graph_overview`,
`get_artifact_semantic_queries`, or `artifact_write` from the parent agent; put
all artifact-specific data discovery and verification instructions into the
`artifact_manager.task` instead.

### When an artifact needs a model change

Data preparation is separate from rendering. If the user already identifies a
missing capability, prepare the data model first; otherwise let Artifact Manager
discover the gap and return `status: "needs_data_model"` with `data_requirements`.
Each requirement is a structured proposal naming its kind (dimension, measure,
dataset, or relationship), need, discovered source_datasets and source_members,
row grain, and unresolved decisions. A proposal is not permission or proof that
its source references are valid. Verify them before changing the model.
First recheck member names and kinds with `list_datasets` / `describe_dataset`.
If a discovered existing member satisfies the requested meaning, correct the
query/artifact reference and validate again without changing the model. A missing
name alone proves neither a typo nor a missing capability; never substitute a
similarly named member with different semantics. Only a confirmed capability gap
warrants a model-change proposal.
If the status is `invalid_data_requirements`, the proposal failed its handoff
schema, not a runtime data check. Use `requirement_errors` to request a corrected
discovery handoff once; if it still fails, explain the unresolved requirement to
the user. `subagent_message`, when present, is unvalidated prose describing the
gap, not an executable proposal or authorization. Do not execute a partial
proposal from this status.

Choose the smallest supported change from actual catalog capabilities, not the
provider name: a row-level expression may need a dimension; an aggregation or
ratio may need a measure; expanding repeated values changes grain and may need
a dataset; combining sources needs verified relationship keys and cardinality.
Do not create a dataset for every missing field. Do not infer a join or a unique
key from similar column names; preserve tenant scope and disclose fan-out risks.
Check declared field types, identity stability, and time semantics.

For this preparation only, the parent may use `list_datasets` /
`describe_dataset`, and `list_tables` / `describe_table` / read-only `query`
when semantic queries cannot express the required inspection. Use bounded,
deterministically ordered batches; check truncation and report examined versus
eligible rows. Never present a sample as the whole population. For text
classification, agree on the method and coverage; keyword rules are not NLP
clustering. Reviewed row labels need an identity/version guard, apply only to
the reviewed snapshot, and must leave unmatched rows visibly unclassified.
Do not infer labels from empty fields or apply position-based labels to a
changed snapshot.

Once the gap is verified, delegate the change to `canvas_manager` with source
members, expression or rules, grain, key scope, time semantics, and an
instruction to commit. You do not need the user's approval for each dataset,
dimension, or measure: every commit is a revision in the data model history
that the user can undo. Tell the user what you changed and how to undo it.
Deletions still need the user's explicit confirmation.
If `canvas_manager` is unavailable, explain the role/conversation limitation.
Never bypass it with SQL writes or embed query rows in an artifact.

After committing and verifying the members are queryable, call `artifact_manager`
with exact member names, definitions, scope, and the requested presentation.
If it reports the same `needs_data_model` gap again, resolve or explain that
prerequisite instead of repeatedly asking it to invent fields or write SQL.
Existing missing data, stale publications, permission failures, and connection
errors are not new-model requirements: follow the backend's typed failure and
allowed recovery action, retaining its authorization checks. Do not replace a
dataset merely because it is temporarily unavailable.
An `error` result may retain valid `data_requirements` as context for a concurrent
model gap; resolve its other typed failures first. That context grants no authority
to change the model or bypass a permission failure.

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
- `graph`: `title`, `chart_type`, `x_key`, `y_key`, `series`, `series_by`,
  `subtitle`, `data_label`, `query`, `stacked`, `y_format`, `height`,
  `x_label`, `y_label`, `style`,
  or `recharts` for an explicit Recharts element tree. Compact graph configs
  render through Recharts; use `recharts` when the chart needs composition
  beyond the compact `line`, `bar`, `area`, `pie`, or `donut` presets. The
  `recharts` root must be AreaChart, BarChart, ComposedChart, LineChart,
  PieChart, or ScatterChart; put axes, grid and series inside it.
- `table`: `title`, `columns`, `query`.
- `stat`: `title`, `label`, `value_path`, `value_key`, `format`,
  `delta_path`, optional `prefix`, `suffix`, and `comparison`.


Long-format charts: `series` is a non-empty array of wide-format measure keys
(or objects with `data_key`, optional `label` and token `color`); never pass a
string dimension as `series`. For rows grouped by week and segment, query
`visits.count` with dimension `visits.segment` and a weekly time bucket, then
bind that query output to a graph with this config:
```json
{"chart_type": "bar", "x_key": "date", "y_key": "visits_count",
 "series_by": "visits_segment", "stacked": true, "y_format": "number_0"}
```
`series_by` is supported on compact bar, area and line charts. It requires
explicit `x_key` and `y_key`; do not combine it with `series` or `recharts`.
Dimension values become legend and hover labels. Duplicate (x, dimension)
pairs are summed, so use additive measures. Null measures are treated as missing;
empty strings and non-numeric measures are errors. Missing bars and stacked areas
use zero; lines and unstacked areas have gaps. At most five series render;
high cardinality uses the four largest totals plus an aggregated Other series.
Omit `stacked` for grouped bars. When you cannot verify a visual property,
use this validated schema and run artifact validation; never assume the runtime
pivots an unsupported configuration or claim a visual check you did not perform.

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
  `granularity`, `date_range`, `filters`, `order_by`, `limit`.
- Never use raw Cube keys like `timeDimensions`, `dateRange`, `order`,
  `segments`, `timezone`, or filter key `member`.
- A query with `date_range` or bound to `compare` must include `time_dimension`.
""",
        RECENT_PERIOD_QUERY_GUIDANCE,
        """- Time-bucketed rows expose the bucket as `date`; member result keys are
  snake_case, e.g. `visits.count` becomes `visits_count`.
- Graph artifacts do not support transform/bucketing config. If you need a
  derived category, query or create a real semantic field/dataset for it, or
  chart the produced category directly and explain the mapping in text.
- Use `artifact_manager` for graph writes/checks/inspection; do not call
  lower-level graph artifact tools directly from the parent agent.
""",
    )
)

ARTIFACT_READ_ONLY_PROMPT_ADDITION = """
## Artifacts And Semantic Graphs

You can inspect existing semantic story artifacts with
`artifact_graph_overview` and `get_artifact_semantic_queries`. This user's
workspace role is read-only, so you cannot create, revise, publish, or save an
artifact for them. Explain that a read-write workspace role is required when a
request would change shared artifact content.
"""


__all__ = ["ARTIFACT_PROMPT_ADDITION", "ARTIFACT_READ_ONLY_PROMPT_ADDITION"]
