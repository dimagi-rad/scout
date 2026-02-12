# Create your first project

A project in Scout represents a single database scope. Each project has its own database connection, agent configuration, table access controls, and knowledge base.

## Create a project

1. Open Scout at `http://localhost:5173` and log in.
2. Click **Projects** in the sidebar, then click **New Project**.
3. Fill in the required fields:

| Field | Description |
|-------|-------------|
| **Name** | Display name for the project (e.g., "Sales Analytics") |
| **Slug** | URL-safe identifier, auto-generated from name |
| **DB Host** | Hostname of the target PostgreSQL database |
| **DB Port** | Port number (default: 5432) |
| **DB Name** | Database name to connect to |
| **DB Schema** | Schema to query (default: `public`) |
| **DB User** | Username for the database connection (encrypted at rest) |
| **DB Password** | Password for the database connection (encrypted at rest) |

4. Click **Save**.

## Configure table access (optional)

By default, the agent can see all tables in the configured schema. To restrict access:

- **Allowed tables** -- JSON list of table names the agent can query. An empty list means all tables are visible.
- **Excluded tables** -- JSON list of table names to hide from the agent, even if they appear in the allowed list.

Example: to limit the agent to only `orders`, `customers`, and `products`:

```json
["orders", "customers", "products"]
```

## Generate the data dictionary

The data dictionary tells the agent what tables and columns exist. Generate it with:

```bash
uv run manage.py generate_data_dictionary --project-slug your-project-slug
```

This introspects the target database and stores the schema information on the project.

## Add team members

Add members to a project via the API:

```bash
# Add a member with a role (viewer, analyst, or admin)
curl -X POST http://localhost:8000/api/projects/<project-id>/members/ \
  -H "Content-Type: application/json" \
  -d '{"email": "user@example.com", "role": "analyst"}'
```

| Role | Permissions |
|------|-------------|
| **Viewer** | Chat with the agent and view results |
| **Analyst** | Chat, export data, create saved queries |
| **Admin** | Full project configuration access |

## Next step

[Start your first conversation](first-conversation.md) to ask the agent a question about your data.
