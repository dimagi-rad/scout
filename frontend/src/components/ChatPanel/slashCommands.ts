export interface SlashCommand {
  name: string
  description: string
  buildPrompt: (args: string) => string
}

export const SLASH_COMMANDS: SlashCommand[] = [
  {
    name: "save-recipe",
    description: "Save this conversation as a reusable recipe",
    buildPrompt: (args) => {
      const base =
        "Create a reusable recipe from this conversation using the save_as_recipe tool. " +
        "Analyze our conversation, extract the key steps, identify values that should become variables for reuse, and save it as a recipe."
      return args ? `${base}\n\n${args}` : base
    },
  },
  {
    name: "refresh-data",
    description: "Pull the latest data from connected accounts",
    buildPrompt: (args) => {
      const base =
        "Trigger a fresh data sync from the connected account using the run_materialization tool. " +
        "Run the appropriate pipeline to pull the latest data, then confirm when the sync is complete."
      return args ? `${base}\n\n${args}` : base
    },
  },
]
