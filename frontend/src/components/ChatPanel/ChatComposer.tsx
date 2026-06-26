import { useState } from "react"
import type { FormEvent, KeyboardEvent } from "react"
import { Send, Square } from "lucide-react"

import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { SLASH_COMMANDS, resolveSlashCommand } from "./slashCommands"
import { SlashCommandMenu } from "./SlashCommandMenu"

interface ChatComposerProps {
  input: string
  setInput: (value: string) => void
  onSend: (text: string) => void
  isStreaming?: boolean
  onStop?: () => void
  placeholder?: string
}

export function ChatComposer({
  input,
  setInput,
  onSend,
  isStreaming = false,
  onStop,
  placeholder = "Ask about your data...",
}: ChatComposerProps) {
  const [slashMenuIndex, setSlashMenuIndex] = useState(0)

  const showSlashMenu =
    !isStreaming && input.startsWith("/") && !input.slice(1).includes(" ")
  const slashQuery = showSlashMenu ? input.slice(1) : ""
  const filteredCommands = SLASH_COMMANDS.filter((cmd) =>
    cmd.name.startsWith(slashQuery),
  )

  function selectSlashCommand(cmd: typeof SLASH_COMMANDS[number]) {
    setInput(`/${cmd.name} `)
    setSlashMenuIndex(0)
  }

  function submit() {
    const text = input.trim()
    if (!text || isStreaming) return
    setInput("")
    onSend(resolveSlashCommand(text))
  }

  function handleSubmit(e: FormEvent) {
    e.preventDefault()
    submit()
  }

  function handleKeyDown(e: KeyboardEvent) {
    if (!showSlashMenu || filteredCommands.length === 0) return

    if (e.key === "ArrowDown") {
      e.preventDefault()
      setSlashMenuIndex((i) => (i + 1) % filteredCommands.length)
    } else if (e.key === "ArrowUp") {
      e.preventDefault()
      setSlashMenuIndex((i) => (i - 1 + filteredCommands.length) % filteredCommands.length)
    } else if (e.key === "Tab" || e.key === "Enter") {
      e.preventDefault()
      selectSlashCommand(filteredCommands[slashMenuIndex])
    }
  }

  return (
    <form onSubmit={handleSubmit} className="relative flex gap-2">
      <SlashCommandMenu
        query={slashQuery}
        onSelect={selectSlashCommand}
        visible={showSlashMenu}
        selectedIndex={slashMenuIndex}
      />
      <Input
        data-testid="chat-input"
        value={input}
        onChange={(e) => {
          setInput(e.target.value)
          setSlashMenuIndex(0)
        }}
        onKeyDown={handleKeyDown}
        placeholder={placeholder}
        disabled={isStreaming}
        className="flex-1"
      />
      {isStreaming ? (
        <Button type="button" variant="outline" size="icon" onClick={onStop}>
          <Square className="w-4 h-4" />
        </Button>
      ) : (
        <Button type="submit" size="icon" disabled={!input.trim()}>
          <Send className="w-4 h-4" />
        </Button>
      )}
    </form>
  )
}
