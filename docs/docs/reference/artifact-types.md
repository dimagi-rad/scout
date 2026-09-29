# Artifact types

Every artifact has an `artifact_type`. The agent creates and updates only
`story` artifacts, through the `artifact_manager` subagent. The other four
types are still stored and rendered, but nothing in Scout creates new ones.

## Story

**Type identifier:** `story`

Stories are semantic-query-backed analyses rendered natively by the frontend
from `data.story_doc`. A story is an ordered list of typed blocks: `title`,
`section`, `question`, `tldr`, `markdown`, `date_filter`, `period_selector`,
`semantic_query`, `graph`, `table`, and `stat`. The backend validates the
document before saving it.

Blocks render vertically by default. Consecutive visible blocks with the same
top-level `row_group` render side by side in a responsive row; use this for KPI
strips, chart pairs, filter rows, and comparison sections. Keep hidden compute
blocks outside the visible row group.

Graph blocks render with Recharts. Compact `line`, `bar`, `area`, `pie`, and
`donut` configurations cover common charts; explicit Recharts element trees
support more advanced composition. Compact charts use a bounded `style`
vocabulary for named palettes, legends, grids, curves, orientation, and value
labels. Stat blocks can show absolute or percent comparisons with a `goal` of
`higher`, `lower`, or `neutral`, so favorable color is applied only when the
metric's meaning supports it.

`semantic_query` blocks hold the live queries. When a story opens, the
frontend calls the artifact's `query-data` endpoint, which runs those queries
against the workspace's semantic model, with any date-filter selections applied.

## Sandboxed types

These types render in a sandboxed iframe served by the artifact's `sandbox/`
endpoint, under a strict Content Security Policy. The artifact's `code` field
holds the source, and `data` is passed to it.

| Type | `code` holds | Rendering |
|------|--------------|-----------|
| `react` | A JSX component | Transformed with Babel and given React, Recharts, D3, lodash and lucide icons, plus `data`. |
| `html` | HTML markup | Injected as-is; `{{key}}` placeholders are filled from `data` and inline scripts run. |
| `markdown` | Markdown text | Rendered with `marked`. |
| `svg` | SVG markup, or D3 code | Markup is inserted directly. Code containing `d3.` or `function` runs with an `svg` root selection, `d3` and `data`. |

## Versioning

Updating a story creates a new artifact row with `version` incremented and
`parent_artifact` pointing at the previous version. Artifact lists show only
the latest visible version of each artifact.

## Fields

- `code`: source for sandboxed types. Empty for stories.
- `data`: structured JSON. For stories it holds `story_doc`.
- `semantic_queries`: the story's valid semantic queries, derived from its
  `semantic_query` blocks.
- `semantic_query_manifest`: per-query validation status, members and datasets,
  used to decide whether the artifact's data can be served or needs repair.
- `source_queries`: legacy SQL-backed queries. They are never executed.
