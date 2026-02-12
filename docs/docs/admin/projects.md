# Projects

A project is the top-level organizational unit in Scout. Each project connects to a specific PostgreSQL database and has its own agent configuration, access controls, and knowledge base.

## Creating a project

Projects are created through the Scout UI. Click **Projects** in the sidebar and then **New Project**.

### Required fields

| Field | Description |
|-------|-------------|
| **Name** | Display name (e.g., "Sales Analytics") |
| **Slug** | URL-safe identifier, must be unique |
| **DB Host** | PostgreSQL hostname |
| **DB Port** | Port number (default: 5432) |
| **DB Name** | Target database name |
| **DB User** | Database username (encrypted at rest) |
| **DB Password** | Database password (encrypted at rest) |

### Optional fields

| Field | Default | Description |
|-------|---------|-------------|
| **DB Schema** | `public` | Schema to scope queries to |
| **Allowed tables** | `[]` (all) | JSON list of allowed table names |
| **Excluded tables** | `[]` (none) | JSON list of excluded table names |
| **System prompt** | empty | Project-specific instructions merged with the base agent prompt |
| **Max rows per query** | 500 | Maximum rows returned per query |
| **Max query timeout** | 30 | Query execution timeout in seconds |
| **LLM model** | `claude-sonnet-4-5-20250929` | LLM model identifier |
| **Readonly role** | empty | PostgreSQL role for read-only access |

## Database credential encryption

Database usernames and passwords are encrypted at rest using Fernet symmetric encryption. The encryption key is set via the `DB_CREDENTIAL_KEY` environment variable. Generate a key with:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

## Agent configuration

### System prompt

Each project can have a custom system prompt that is merged with the base agent prompt. Use this to provide project-specific context:

- Domain-specific terminology.
- Default assumptions (e.g., "all monetary amounts are in USD cents").
- Preferred output formats.

### LLM model

The `llm_model` field sets which Anthropic model the agent uses. The default is `claude-sonnet-4-5-20250929`.

### Query limits

- **Max rows per query** -- protects against accidentally retrieving massive result sets.
- **Max query timeout** -- kills long-running queries to avoid tying up database connections. The timeout is enforced via PostgreSQL's `statement_timeout`.

## Project status

Projects have an `is_active` flag. Inactive projects cannot be used for chat conversations. Deactivating a project does not delete its data.

## Data dictionary

After creating a project, generate the data dictionary so the agent knows what tables and columns exist:

```bash
uv run manage.py generate_data_dictionary --project-slug your-slug
```

See [Data dictionary](data-dictionary.md) for details.
