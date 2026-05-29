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

/**
 * Resolve raw input text into the prompt that should actually be sent.
 *
 * If the text begins with a recognized slash command (e.g. `/refresh-data foo`),
 * returns the command's built prompt. Otherwise returns the original text
 * unchanged. Shared by both the active-thread input and the empty-state input so
 * slash commands behave identically in both.
 */
export function resolveSlashCommand(text: string): string {
  if (!text.startsWith("/")) return text
  const spaceIdx = text.indexOf(" ")
  const cmdName = spaceIdx === -1 ? text.slice(1) : text.slice(1, spaceIdx)
  const args = spaceIdx === -1 ? "" : text.slice(spaceIdx + 1).trim()
  const cmd = SLASH_COMMANDS.find((c) => c.name === cmdName)
  return cmd ? cmd.buildPrompt(args) : text
}
