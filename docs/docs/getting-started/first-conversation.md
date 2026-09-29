# Start your first conversation

Once a workspace has a connected data source and its data has been loaded (see [Testing CommCare integration in development](dev-testing.md)), you can start asking questions.

## Open the chat

1. Go to `http://localhost:5173` and log in.
2. Choose a workspace with the workspace switcher.
3. Type a question in the chat input and press Enter.

## Example questions

Try starting with simple, concrete questions about your data:

- "What data is available in this workspace?"
- "How many cases were opened last month?"
- "Show me form submissions per week for the last quarter"

## What happens behind the scenes

When you send a message, Scout:

1. **Checks access** -- you must be a member of the workspace, and your role decides which tools the agent can use.
2. **Sends** your message to the LangGraph agent with context about your workspace's semantic catalog, knowledge base, and agent learnings.
3. **Builds a semantic query** -- the agent chooses curated datasets, measures, dimensions, filters, and limits.
4. **Validates the request** -- requested semantic members must exist and be visible in the workspace model.
5. **Executes the semantic query** -- Scout compiles the structured request into a trusted backend query with row limits and a statement timeout. When the semantic model can't express a question, the agent can fall back to read-only SQL.
6. **Returns results** -- the agent formats the results as a response, which may include tables, explanations, or artifacts like charts.

## Understanding the response

The agent's response can include:

- **Text explanations** -- natural language description of the results.
- **Data tables** -- formatted query results.
- **Artifacts** -- interactive charts, dashboards, or visualizations. These appear in the artifact viewer panel.
- **Semantic query provenance** -- the agent can explain the dataset, measures, dimensions, filters, and time range behind an answer.

## Errors and corrections

If a query fails, the agent reads the error's classification and responds to it: it fixes the query when the request was invalid, asks for access or an admin's help when that is what's needed, and retries only transient failures. When it discovers a correction worth keeping, it can save it as a learning that is included in later conversations in the workspace.

## Conversation history

Conversations are persisted using a PostgreSQL checkpointer. You can continue a conversation where you left off -- the agent remembers the context from earlier messages in the same thread.

## Slash commands

Type `/` in the chat input to see available commands. For example, `/save-recipe` saves the current conversation as a reusable recipe. See [Asking questions](../guide/asking-questions.md#slash-commands) for the full list.

## Next steps

- [Asking questions](../guide/asking-questions.md) -- tips for getting better results
- [Understanding results](../guide/understanding-results.md) -- how to read responses, tables, and errors
- [Artifacts](../guide/artifacts.md) -- charts, dashboards, and interactive visualizations
