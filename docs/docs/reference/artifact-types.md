# Artifact types

Scout supports several artifact types, each suited to different visualization and reporting needs.

## React

**Type identifier:** `react`

Interactive React components rendered in a sandboxed iframe. Best for complex, interactive visualizations and dashboards.

The agent writes a React component that receives the query data as props. The component is rendered in an isolated sandbox with access to common libraries, including Recharts for charts.

**Use cases:**
- Multi-panel dashboards
- Interactive data explorers
- Custom visualizations with user controls
- Forms and filters

## HTML

**Type identifier:** `html`

Static HTML documents rendered in a sandboxed iframe. Best for formatted reports and styled tables.

**Use cases:**
- Formatted reports with custom styling
- Styled data tables
- Summary pages

## Markdown

**Type identifier:** `markdown`

Markdown documents rendered to HTML. Best for narrative reports and text-heavy content.

**Use cases:**
- Analysis write-ups
- Data summaries
- Narrative reports with embedded tables

## SVG

**Type identifier:** `svg`

SVG graphics rendered inline. Best for diagrams and simple illustrations.

**Use cases:**
- Entity relationship diagrams
- Flowcharts
- Simple data graphics

## Story

**Type identifier:** `story`

Semantic-query-backed analysis stories rendered from `data.story_doc`. A story is an ordered list of typed blocks
such as `title`, `section`, `date_filter`, `semantic_query`, `graph`, `table`, and `stat`.

Blocks render vertically by default. Consecutive visible blocks with the same top-level `row_group` render side by
side in a responsive row; use this for KPI strips, chart pairs, filter rows, and comparison sections. Keep hidden
compute blocks outside the visible row group.

Story graph blocks render with Recharts. Compact `line`, `bar`, `area`, `pie`, and `donut` configurations cover common charts; explicit Recharts element trees support more advanced composition. Compact charts use a bounded `style` vocabulary for named palettes, legends, grids, curves, orientation, and value labels. Stat blocks can show absolute or percent comparisons with a semantic higher/lower/neutral goal, so favorable color is only applied when metric meaning supports it.

## Versioning

All artifact types support versioning. When the agent creates an updated version of an artifact, it links the new version to the original via the `parent_artifact` field. The version number is automatically incremented.

## Data field

Artifacts have a `data` JSON field that stores structured data used by the artifact. For example:

- **React** artifacts may store static data that the component renders.
- **Story** artifacts store a structured story document and named semantic query specs for live data.

The `code` field contains the source code (React JSX, HTML markup, Markdown text, or SVG markup), and the `data` field contains supplementary structured data.

## Semantic query provenance

Story artifacts track the named `semantic_queries` that provide their live data.
Legacy SQL-backed `source_queries` are disabled and are not executed.
