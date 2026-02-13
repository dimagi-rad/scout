# Asking questions

Scout translates natural language questions into SQL queries. The quality of the results depends on how you phrase your questions and the knowledge available to the agent.

## Tips for good questions

### Be specific about what you want

Instead of "show me some data", ask "show me the top 10 customers by total order amount in the last 30 days". The more specific your question, the more accurate the SQL will be.

### Name tables and columns when you know them

If you know the table or column names, include them: "What is the average `order_total` from the `orders` table?" This reduces ambiguity and helps the agent generate correct SQL on the first try.

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

- **Data dictionary** -- auto-generated schema documentation listing all visible tables, columns, and their types.
- **Knowledge entries** -- markdown documents covering metric definitions, business rules, verified queries, and other institutional knowledge.
- **Table knowledge** -- human-written descriptions of what tables mean, use cases, and data quality notes.
- **Agent learnings** -- corrections the agent has discovered from previous errors.

The more knowledge you add to a project, the better the agent's answers become.

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
