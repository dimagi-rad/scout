# Recipes

Recipes are reusable analysis workflows. They let you save a sequence of prompts with variables, so anyone on the team can re-run common analyses without writing prompts from scratch.

## Concepts

A recipe consists of:

- **Name and description** -- what the recipe does.
- **Variables** -- parameters that change between runs (e.g., date range, region, limit).
- **Steps** -- ordered prompt templates with `{{variable}}` placeholders.

## Variable types

Variables are defined with a type, label, and optional default value:

| Type | Description |
|------|-------------|
| `string` | Free-form text |
| `number` | Numeric value |
| `date` | Date value |
| `boolean` | True/false |
| `select` | Choose from a predefined list of options |

Example variable definition:

```json
{
  "name": "region",
  "type": "select",
  "label": "Region",
  "options": ["North", "South", "East", "West"],
  "default": "North"
}
```

## Steps

Each step has a prompt template with `{{variable}}` placeholders that get replaced with actual values at run time:

```
Show me the top {{limit}} customers from the {{region}} region
for the period {{start_date}} to {{end_date}}.
```

Steps also have an optional `expected_tool` field (e.g., `execute_sql`) that indicates what tool the agent should use.

## Creating a recipe

The easiest way to create a recipe is with the `/save-recipe` slash command. After an analysis conversation, type:

```
/save-recipe
```

The agent will review the conversation, extract the key steps, identify values that should become variables, and save it as a recipe. You can also add instructions to guide the extraction:

```
/save-recipe focus on the monthly revenue breakdown, make the date range a variable
```

Alternatively, you can ask the agent in plain language to save a recipe -- it has access to the `save_as_recipe` tool.

Recipes can also be created and managed from the **Recipes** page in the sidebar.

## Running a recipe

When you run a recipe, you provide values for each variable. The system:

1. Validates the variable values against the recipe's definitions.
2. Renders each step's prompt template by substituting the variable values.
3. Sends each step to the agent in sequence.
4. Records the results of each step, including the response and any tools used.

## Run tracking

Each recipe run is tracked with:

- The variable values used.
- The status (pending, running, completed, failed).
- Results from each step.
- Timing information.

## Sharing recipes

Recipes can be shared with all project members by setting the `is_shared` flag. Shared recipes are visible to everyone in the project. Unshared recipes are only visible to the creator.
