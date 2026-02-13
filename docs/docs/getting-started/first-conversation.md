# Start your first conversation

Once you have a project configured with a database connection and a generated data dictionary, you can start asking questions.

## Open the chat

1. Go to `http://localhost:5173` and log in.
2. Select your project from the project selector.
3. Type a question in the chat input and press Enter.

## Example questions

Try starting with simple, concrete questions about your data:

- "What tables are available?"
- "How many rows are in the orders table?"
- "Show me the top 10 customers by total revenue"
- "What was the total revenue last month?"

## What happens behind the scenes

When you send a message, Scout:

1. **Validates** your project membership and permissions.
2. **Sends** your message to the LangGraph agent with context about your project's schema, knowledge base, and agent learnings.
3. **Generates SQL** -- the agent writes a SQL query based on your question and the data dictionary.
4. **Validates the query** -- only SELECT statements are allowed; dangerous functions are blocked; table access controls are enforced.
5. **Executes the query** -- the query runs against your database with a read-only connection, row limits, and a statement timeout.
6. **Returns results** -- the agent formats the results as a response, which may include tables, explanations, or artifacts like charts.

## Understanding the response

The agent's response can include:

- **Text explanations** -- natural language description of the results.
- **Data tables** -- formatted query results.
- **Artifacts** -- interactive charts, dashboards, or visualizations. These appear in the artifact viewer panel.
- **SQL queries** -- the agent shows the SQL it ran so you can verify the logic.

## Self-correction

If a query fails (for example, due to a wrong column name), the agent automatically retries with corrections, up to three times. It learns from these corrections and applies them to future queries.

## Conversation history

Conversations are persisted using a PostgreSQL checkpointer. You can continue a conversation where you left off -- the agent remembers the context from earlier messages in the same thread.

## Slash commands

Type `/` in the chat input to see available commands. For example, `/save-recipe` saves the current conversation as a reusable recipe. See [Asking questions](../guide/asking-questions.md#slash-commands) for the full list.

## Next steps

- [Asking questions](../guide/asking-questions.md) -- tips for getting better results
- [Understanding results](../guide/understanding-results.md) -- how to read responses, tables, and errors
- [Artifacts](../guide/artifacts.md) -- charts, dashboards, and interactive visualizations
