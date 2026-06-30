# Artifact types

Scout supports five artifact types, each suited to different visualization and reporting needs.

## React

**Type identifier:** `react`

Interactive React components rendered in a sandboxed iframe. Best for complex, interactive visualizations and dashboards.

The agent writes a React component that receives the query data as props. The component is rendered in an isolated sandbox with access to common libraries.

**Use cases:**
- Multi-panel dashboards
- Interactive data explorers
- Custom visualizations with user controls
- Forms and filters

## Plotly

**Type identifier:** `plotly`

[Plotly.js](https://plotly.com/javascript/) charts rendered in a sandboxed iframe. Best for standard data visualizations.

The agent generates a Plotly chart specification (data + layout) in JSON. The chart is rendered with full interactivity: zoom, pan, hover tooltips, and legend toggles.

**Use cases:**
- Bar charts, line charts, scatter plots
- Heatmaps, histograms
- Time series visualizations
- Statistical plots

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

## Versioning

All artifact types support versioning. When the agent creates an updated version of an artifact, it links the new version to the original via the `parent_artifact` field. The version number is automatically incremented.

## Data field

Artifacts have a `data` JSON field that stores structured data used by the artifact. For example:

- **Plotly** artifacts store chart data and layout configuration.
- **React** artifacts may store static data that the component renders.
- **Story** artifacts store a structured story document and named semantic query specs for live data.

The `code` field contains the source code (React JSX, HTML markup, Markdown text, Plotly JSON, or SVG markup), and the `data` field contains supplementary structured data.

## Semantic query provenance

Story artifacts track the named `semantic_queries` that provide their live data.
Legacy SQL-backed `source_queries` are disabled and are not executed.
