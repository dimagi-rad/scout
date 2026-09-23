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
        "Refresh data from the workspace's connected accounts. Respect the current materialization status: " +
        "if a refresh is already in progress, do not start another one; explain that it is running and end this turn. " +
        "Otherwise call run_materialization once and report its actual result. " +
        "If the result is started, acknowledge that the background job has started and end this turn; " +
        "do not claim the sync is complete or query the new data yet. " +
        "Only promise an automatic follow-up when the tool confirms a job for this conversation. " +
        "If the tool reports failure, explain the required next step instead of promising completion."
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
