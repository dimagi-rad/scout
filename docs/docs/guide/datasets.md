# Datasets

The agent answers questions through a **semantic model**: a set of datasets, each with named measures (values you can sum, count, or average) and dimensions (values you can group or filter by, including time dimensions). Scout builds the model from the data it loads from your workspace's data sources and serves it through Cube.

## Browsing datasets

Open **Datasets** in the sidebar to see the model for the current workspace. Pick a dataset to see:

- Its label, underlying table, and the row count recorded by the most recent data load.
- **Measures** and **Dimensions**, with each member's type, value format, and description.
- **Relationships** to other datasets, which let a query combine them. A relationship that the last model build skipped is flagged, because queries cannot use it.
- For a custom dataset, the SQL that defines it.

Use the names you see here when asking questions (see [Asking questions](asking-questions.md#name-datasets-and-fields-when-you-know-them)).

## Custom datasets and fields

The agent can add custom datasets (defined by SQL over the loaded tables) and custom fields to the model. It stages these changes on the conversation's **Canvas**, which opens in a side panel in the chat. The changes take effect once they are saved, either by the agent or with **Save all** on the Canvas, which rebuilds the semantic model.

Changing the model requires the **Read-Write** or **Manager** workspace role. **Read** members can view the Canvas but not save it.

## Keeping data current

Datasets reflect the most recent data load. Members with write access can load the latest data with the `/refresh-data` slash command (see [Asking questions](asking-questions.md#slash-commands)).
