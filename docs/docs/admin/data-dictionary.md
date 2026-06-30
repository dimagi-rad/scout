# Data dictionary management

The `generate_data_dictionary` management command is a legacy schema-documentation tool. Semantic-model mode uses the dataset browser and semantic catalog instead of giving the agent raw schema context for SQL generation.

## Usage

```bash
# Generate for a specific project
uv run manage.py generate_data_dictionary --project-slug my-project

# Generate for all projects
uv run manage.py generate_data_dictionary --all

# Dry run -- print to stdout without saving
uv run manage.py generate_data_dictionary --project-slug my-project --dry-run
```

## What it generates

The command connects to the project's target database (using the encrypted credentials stored on the project) and introspects the schema to capture:

- Table names in the configured schema.
- Column names, data types, and nullability.
- Primary key and foreign key relationships.
- Constraints and indexes.

The result is stored as a JSON field on the project model (`data_dictionary`) along with a timestamp (`data_dictionary_generated_at`).

## Table access filtering

The generated data dictionary respects the project's table access settings. Only tables that pass the allowlist/blocklist check are included. See [Table access](table-access.md).

## Keeping it up to date

The data dictionary is a point-in-time snapshot. It does not automatically update when the target database schema changes. Regenerate it whenever:

- New tables are added.
- Columns are added, renamed, or removed.
- Data types change.
- Foreign key relationships change.

Consider adding the command to a periodic job (e.g., a daily cron or CI pipeline) for databases with frequently changing schemas.

## Dry run

The `--dry-run` flag prints the rendered data dictionary to stdout without saving it. Use this to preview what the agent will see:

```bash
uv run manage.py generate_data_dictionary --project-slug my-project --dry-run
```
