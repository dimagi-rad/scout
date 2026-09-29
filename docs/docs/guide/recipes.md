# Recipes

A recipe is a saved, reusable analysis: a prompt template with variables. Anyone in the workspace can re-run it with different values instead of rewriting the prompt.

## What a recipe contains

- **Name and description** -- what the recipe does.
- **Prompt** -- a markdown prompt template with `{{variable}}` placeholders.
- **Variables** -- the values that change between runs, such as a date range or region.

## Variables

Each variable has a `name`, a `type`, a `label`, and an optional `default`. The supported types are:

| Type | Accepted values |
|------|-----------------|
| `string` | Any text |
| `number` | Any value that parses as a number |
| `date` | A date in `YYYY-MM-DD` format |
| `boolean` | `true`/`false`, `1`/`0`, or `yes`/`no` |
| `select` | One of the variable's `options` (required for this type) |

A variable without a default must be given a value when the recipe runs. Example definition:

```json
{
  "name": "region",
  "type": "select",
  "label": "Region",
  "options": ["North", "South", "East", "West"],
  "default": "North"
}
```

At run time each `{{name}}` placeholder in the prompt is replaced with the variable's value:

```
Show me the top {{limit}} customers from the {{region}} region
for the period {{start_date}} to {{end_date}}.
```

## Creating a recipe

Recipes are created by the agent during a conversation. After an analysis, type:

```
/save-recipe
```

The agent reviews the conversation, writes a prompt template, turns the values worth changing into variables, and saves the recipe with its `save_as_recipe` tool. You can add instructions after the command:

```
/save-recipe focus on the monthly revenue breakdown, make the date range a variable
```

You can also ask in plain language ("save this as a recipe"). The agent rejects a recipe whose prompt uses a `{{placeholder}}` that isn't defined as a variable.

Saving a recipe requires the **Read-Write** or **Manager** workspace role. For **Read** members the `/save-recipe` command is hidden and the agent does not have the `save_as_recipe` tool.

## Managing recipes

The **Recipes** page in the sidebar lists the workspace's recipes. Members with write access can edit a recipe's name, description, and prompt, and delete recipes. Variables are shown read-only in the UI. There is no button to create a recipe from scratch on this page.

## Running a recipe

Click **Run** on a recipe, fill in the variables, and click **Run Recipe**. Any workspace member, including **Read** members, can run a recipe.

When a run starts, Scout:

1. Validates the values against the variable definitions (and fills in defaults). Invalid values are rejected before anything runs.
2. Substitutes the values into the prompt.
3. Queues the run as a background job, which sends the rendered prompt to the agent as a single non-interactive request.

The agent in a run has the same tool access as the member who started it. A **Read** member's run cannot save recipes, save learnings, or refresh data.

## Run history

Each run records:

- The variable values used.
- Its status: `pending`, `running`, `completed`, or `failed`.
- The rendered prompt, the agent's response, the tools it used, and any artifacts it created.
- Start and completion times.

The Recipes page polls while a run is pending or running, so results appear without reloading.

## Visibility

Recipes and runs belong to the workspace, and every member of the workspace can see all of them. There is no per-recipe or per-run sharing setting.
