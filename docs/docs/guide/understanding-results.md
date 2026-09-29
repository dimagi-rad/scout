# Understanding results

When you ask a question, the agent answers in text and shows the tool calls it made along the way. Some answers also produce an artifact.

## Text responses

The agent's written answer summarizes what it found and notes caveats about the data. If it used SQL instead of a semantic query, it should say so and why.

## Tool calls and query results

Each tool call appears in the conversation as a collapsible row. Expand it to see the details:

- **Semantic queries** show the rows returned, the row count and timing, the semantic query that ran (datasets, measures, dimensions, filters), and the members it used.
- **SQL queries** show the rows returned, the row count, timing, and tables accessed, with a **SQL** tab showing the exact SQL that ran.

A **truncated** badge means the result hit the row limit (semantic queries return 100 rows by default and at most 500; SQL queries at most 500). Ask for a narrower filter or an aggregate if you need the full picture.

## Artifacts

When the agent builds a chart or dashboard, it appears as an artifact button in the chat. Click it to open the artifact. See [Artifacts](artifacts.md).

## Errors

A failed tool call shows its error message and code. Common ones:

- **Validation errors** -- the query referenced an unknown dataset or member, used an unsupported filter, or the SQL failed safety checks.
- **Timeouts** -- the query ran longer than 30 seconds. Add filters or aggregate to reduce the data scanned.
- **Access errors** -- your account's access to a data source has expired or been removed. Reconnect it on the **Connected Accounts** page.

The agent usually reads the error, adjusts its query, and tries again. It is told to limit retries, and to stop and explain what it needs when it can't find the right fields rather than guessing.

If the AI service is briefly overloaded, Scout automatically retries your message once and shows a message only if the retry also fails. If you send messages too quickly (more than 20 a minute), Scout asks you to wait before sending another.

## Learnings

When the agent discovers a correction worth keeping, such as a filter a metric always needs, it can save it as a learning. Learnings are included in later conversations in the same workspace. See [Knowledge](../admin/knowledge.md#agent-learnings).
