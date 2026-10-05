# Artifacts

Artifacts are the reports and dashboards the agent builds from your data: charts, stat tiles, tables, and narrative text on one page.

## Stories

The agent creates artifacts as **stories**. A story is a document of blocks:

- **Text**: titles, sections, the question being answered, a TL;DR summary, and markdown.
- **Data**: charts (line, bar, area, pie, donut, and composed Recharts charts), stat tiles with optional comparisons, and tables.
- **Controls**: date filters and period selectors.

Each data block is backed by a semantic query, not a stored snapshot. The queries run each time the artifact is displayed, so a story reflects the latest loaded data.

Older workspaces may also contain artifacts of the legacy types `react`, `html`, `markdown`, and `svg`. These still render, in a sandboxed iframe, but the agent no longer creates them.

## Requesting artifacts

Ask for a chart or dashboard in chat:

- "Chart the monthly visit trend"
- "Create a dashboard showing key metrics for this quarter"
- "Build a bar chart comparing sales by region"

The main agent hands artifact work to a subagent, the **Artifact editor**, which checks the semantic queries and writes the story. Creating or changing artifacts requires the **Read-Write** or **Manager** workspace role. For **Read** members the agent can inspect existing artifacts but cannot create or change them.

## Viewing artifacts

Artifacts created in a conversation appear as buttons in the chat and in the thread's **Artifacts** side panel. Click one to open it. Every artifact in the workspace is listed on the **Artifacts** page in the sidebar, which has a search box.

The artifact viewer has two actions:

- **View Data** shows the rows behind each of the story's queries.
- **Export PDF** opens your browser's print dialog for the artifact, where you can save it as a PDF.

If an artifact's data is unavailable, for example because the data model changed or a data load is incomplete, the viewer shows a banner explaining why and, where possible, a button to rebuild or restore the data. The button is disabled for **Read** members.

## Changing artifacts

To change an artifact, ask the agent ("add a filter for date range"). Each change saves a new version linked to the original, and the Artifacts page shows the latest version.

On the Artifacts page, **Read-Write** and **Manager** members can also edit an artifact's title and description, or delete it.
