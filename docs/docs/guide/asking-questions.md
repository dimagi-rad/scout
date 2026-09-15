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

The agent has access to:

- **Dataset browser** -- semantic model documentation listing visible datasets, measures, dimensions, and time dimensions.
- **Knowledge entries** -- markdown documents covering metric definitions, business rules, and other institutional knowledge.
- **Table knowledge** -- human-written descriptions of what source tables mean, use cases, and data quality notes.
- **Agent learnings** -- corrections the agent has discovered from previous errors.

The more knowledge you add to a project, the better the agent's answers become.

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
read-write or manage workspace role. The Data Model canvas owns this change;
the Artifact Manager uses the saved semantic fields to create and validate the
chart. A request for a chart alone does not authorize a model change.

Keyword matching is not NLP clustering. Reviewed message-ID labels are a
snapshot, so refreshes do not automatically label new messages. Do not treat
topics observed in a sample as verified totals for all messages. A live
dashboard re-runs its saved semantic queries; it does not re-run free-text
topic extraction on every refresh.

## Slash commands

Type `/` at the start of the chat input to see available slash commands. An autocomplete menu appears as you type -- use arrow keys to navigate and Tab or Enter to select.

| Command | Description |
|---------|-------------|
| `/save-recipe` | Save the current conversation as a reusable recipe. Optionally add instructions after the command, e.g. `/save-recipe make the date range a variable`. |

After selecting a command, press Enter to execute it. The command is translated into a prompt for the agent behind the scenes.

## Limitations

- The agent can only run **SELECT** queries. It cannot insert, update, or delete data.
- Results are limited to a configurable maximum number of rows (default: 500).
- Queries have a timeout (default: 30 seconds).
- Some PostgreSQL functions are blocked for security reasons (file access, remote connections, etc.).
