# Asking questions

Scout translates natural language questions into structured semantic queries. The quality of the results depends on how you phrase your questions and the semantic model available to the agent.

## Tips for good questions

### Be specific about what you want

Instead of "show me some data", ask "show me the top 10 customers by total order amount in the last 30 days". The more specific your question, the easier it is for the agent to choose the right dataset, measures, dimensions, and filters.

### Name datasets and fields when you know them

If you know the dataset or field names, include them: "What is the average `order_total` from the `orders` dataset?" This reduces ambiguity and helps the agent choose the correct semantic members on the first try.

### Specify time ranges explicitly

"Last month's revenue" is ambiguous -- does it mean the last 30 days, or the previous calendar month? Be explicit: "Total revenue for January 2026" or "Total revenue for the last 30 days".

### Ask follow-up questions

Conversations are persistent. After getting an initial result, you can ask follow-ups:

- "Break that down by region"
- "Now show only the top 5"
- "Can you chart that?"
- "Exclude cancelled orders"

The agent remembers the context from earlier in the conversation.

### Request specific output formats

You can ask for specific formats:

- "Show me a bar chart of monthly revenue"
- "Create a dashboard comparing this quarter to last quarter"
- "Give me a table sorted by date descending"

## What the agent knows

In every conversation the agent has:

- **The semantic model** -- the workspace's datasets, measures, dimensions, and relationships, which you can browse on the [Datasets](datasets.md) page.
- **Workspace instructions** -- the workspace's system prompt, set by a manager.
- **Knowledge** -- the workspace's knowledge entries, table notes, and learnings the agent has saved from earlier corrections. See [Knowledge](../admin/knowledge.md).

The agent prefers semantic queries. When a question needs something the model can't express, such as reading free-text columns, it can fall back to a read-only SQL query and should say that it did.

## Topics in Open Chat Studio transcripts

Topic analysis has two separate steps: inspect the message text, then save a
queryable topic definition before building a reusable dashboard. Empty session
tags do not mean the text is unavailable. Scout can inspect raw message content
using read-only SQL when a semantic query cannot express the analysis.

Start with a request such as:

> Inspect the user messages for July and propose topics from the actual text.
> Include message IDs, state how many eligible messages you examined, and tell
> me if the results are sampled or truncated. Do not save a data model yet.

After reviewing the classification method and coverage, explicitly request the
model change and the chart:

> Create and save a `message_topics` dataset using the reviewed rules. Keep one
> row per nonblank user message, its source message ID as the primary key, and
> its timestamp for date filters. Keep unmatched messages as unclassified.
> Then build a dashboard of message count by topic, clearly labeled with the
> classification method and date range.

The actual source table and column names must be discovered in your workspace;
multi-chatbot workspaces may use prefixed names. Saving a dataset requires a
Read-Write or Manager workspace role. The change is staged on the chat's Canvas
and saved to the data model (see [Datasets](datasets.md#custom-datasets-and-fields));
the Artifact editor uses the saved semantic fields to create and validate the
chart. A request for a chart alone does not authorize a model change.

Keyword matching is not NLP clustering. Reviewed message-ID labels are a
snapshot, so refreshes do not automatically label new messages. Do not treat
topics observed in a sample as verified totals for all messages. A live
dashboard re-runs its saved semantic queries; it does not re-run free-text
topic extraction on every refresh.

## Slash commands

Type `/` at the start of the chat input to see the available slash commands. The menu filters as you type; use the arrow keys to move and Tab or Enter to complete the command. Add any extra instructions after it, then press Enter to send. Scout expands the command into a prompt for the agent.

| Command | Description |
|---------|-------------|
| `/save-recipe` | Save the current conversation as a reusable [recipe](recipes.md). Optionally add instructions, e.g. `/save-recipe make the date range a variable`. |
| `/refresh-data` | Load the latest data from the workspace's connected accounts. The load runs in the background; the agent reports that it has started, or that a load is already running. |

Both commands need the **Read-Write** or **Manager** workspace role and are hidden from **Read** members.

## Limitations

- The agent cannot change your source data. Semantic queries are read-only, and SQL queries must be a single `SELECT` that runs under a read-only database role.
- SQL queries can only read the workspace's own schemas, not system catalogs, and can only call functions on an allowlist of analytics functions. File access, remote connections, and similar functions are blocked.
- Semantic queries return 100 rows by default and at most 500. SQL queries return at most 500 rows. The agent is told when results are truncated.
- Database queries time out after 30 seconds. A semantic query can take up to about a minute before failing, because Cube adds compile and polling time.
- Each user can send up to 20 chat messages per minute.
