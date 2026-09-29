import type { SlashCommand } from "./slashCommands"
import { matchSlashCommands } from "./slashCommands"

interface SlashCommandMenuProps {
  query: string
  onSelect: (cmd: SlashCommand) => void
  visible: boolean
  selectedIndex: number
  canWrite?: boolean
}

export function SlashCommandMenu({ query, onSelect, visible, selectedIndex, canWrite = true }: SlashCommandMenuProps) {
  const filtered = matchSlashCommands(query, canWrite)

  if (!visible || filtered.length === 0) return null

  return (
    <div
      data-testid="slash-command-menu"
      className="absolute bottom-full mb-1 left-0 w-full rounded-md border bg-popover p-1 shadow-md"
    >
      {filtered.map((cmd, i) => (
        <button
          key={cmd.name}
          data-testid={`slash-command-${cmd.name}`}
          className={`flex w-full items-center gap-2 rounded-sm px-2 py-1.5 text-sm text-left cursor-pointer ${
            i === selectedIndex ? "bg-accent" : ""
          }`}
          onMouseDown={(e) => {
            e.preventDefault()
            onSelect(cmd)
          }}
        >
          <span className="font-semibold">/{cmd.name}</span>
          <span className="text-muted-foreground">{cmd.description}</span>
        </button>
      ))}
    </div>
  )
}
