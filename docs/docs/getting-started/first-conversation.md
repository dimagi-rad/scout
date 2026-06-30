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
2. **Sends** your message to the LangGraph agent with context about your workspace's semantic catalog, knowledge base, and agent learnings.
3. **Builds a semantic query** -- the agent chooses curated datasets, measures, dimensions, filters, and limits.
4. **Validates the request** -- requested semantic members must exist and be visible in the workspace model.
5. **Executes the semantic query** -- Scout compiles the structured request into a trusted backend query with row limits and a statement timeout.
6. **Returns results** -- the agent formats the results as a response, which may include tables, explanations, or artifacts like charts.

## Understanding the response

The agent's response can include:

- **Text explanations** -- natural language description of the results.
- **Data tables** -- formatted query results.
- **Artifacts** -- interactive charts, dashboards, or visualizations. These appear in the artifact viewer panel.
- **Semantic query provenance** -- the agent can explain the dataset, measures, dimensions, filters, and time range behind an answer.

## Self-correction

If a semantic query fails (for example, due to an unknown member), the agent automatically retries with corrections, up to three times. It learns from these corrections and applies them to future queries.

## Conversation history

Conversations are persisted using a PostgreSQL checkpointer. You can continue a conversation where you left off -- the agent remembers the context from earlier messages in the same thread.

## Slash commands

Type `/` in the chat input to see available commands. For example, `/save-recipe` saves the current conversation as a reusable recipe. See [Asking questions](../guide/asking-questions.md#slash-commands) for the full list.

## Next steps

- [Asking questions](../guide/asking-questions.md) -- tips for getting better results
- [Understanding results](../guide/understanding-results.md) -- how to read responses, tables, and errors
- [Artifacts](../guide/artifacts.md) -- charts, dashboards, and interactive visualizations
