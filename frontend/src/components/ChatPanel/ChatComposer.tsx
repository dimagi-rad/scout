import { useState } from "react"
import type { FormEvent, KeyboardEvent } from "react"
import { Plus, Send, Square } from "lucide-react"

import { Button } from "@/components/ui/button"
import { Textarea } from "@/components/ui/textarea"
import { useWorkspaceRole } from "@/hooks/useWorkspaceRole"
import { matchSlashCommands, resolveSlashCommand, type SlashCommand } from "./slashCommands"
import { SlashCommandMenu } from "./SlashCommandMenu"

interface ChatComposerProps {
  input: string
  setInput: (value: string) => void
  onSend: (text: string) => void
  isStreaming?: boolean
  onStop?: () => void
  placeholder?: string
  /** "add" while the chat's data loads: the text joins the held request instead. */
  mode?: "send" | "add"
  /** The chat can't start a turn yet (its history is loading); typing still works. */
  sendBlocked?: boolean
  /** Why the send is blocked, when it isn't the history loading. */
  sendBlockedReason?: string
}

export function ChatComposer({
  input,
  setInput,
  onSend,
  isStreaming = false,
  onStop,
  placeholder = "Ask about your data...",
  mode = "send",
  sendBlocked = false,
  sendBlockedReason = "Loading the conversation...",
}: ChatComposerProps) {
  const adding = mode === "add"
  const blocked = sendBlocked && !adding
  const [slashMenuIndex, setSlashMenuIndex] = useState(0)
  const { canWrite } = useWorkspaceRole()

  const showSlashMenu = input.startsWith("/") && !/\s/.test(input.slice(1))
  const slashQuery = showSlashMenu ? input.slice(1) : ""
  const filteredCommands = matchSlashCommands(slashQuery, canWrite)

  function selectSlashCommand(cmd: SlashCommand) {
    setInput(`/${cmd.name} `)
    setSlashMenuIndex(0)
  }

  function submit() {
    const text = input.trim()
    if (!text || isStreaming || blocked) return
    setInput("")
    onSend(resolveSlashCommand(text))
  }

  function handleSubmit(e: FormEvent) {
    e.preventDefault()
    submit()
  }

  function handleKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.nativeEvent.isComposing) return

    if (showSlashMenu && filteredCommands.length > 0) {
      if (e.key === "ArrowDown") {
        e.preventDefault()
        setSlashMenuIndex((i) => (i + 1) % filteredCommands.length)
        return
      }
      if (e.key === "ArrowUp") {
        e.preventDefault()
        setSlashMenuIndex((i) => (i - 1 + filteredCommands.length) % filteredCommands.length)
        return
      }
      if (e.key === "Tab" || (e.key === "Enter" && !e.shiftKey)) {
        e.preventDefault()
        selectSlashCommand(filteredCommands[Math.min(slashMenuIndex, filteredCommands.length - 1)])
        return
      }
    }

    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault()
      submit()
    }
  }

  return (
    <form onSubmit={handleSubmit} className="relative flex gap-2">
      <SlashCommandMenu
        query={slashQuery}
        onSelect={selectSlashCommand}
        visible={showSlashMenu}
        selectedIndex={slashMenuIndex}
        canWrite={canWrite}
      />
      <Textarea
        data-testid="chat-input"
        rows={1}
        value={input}
        onChange={(e) => {
          setInput(e.target.value)
          setSlashMenuIndex(0)
        }}
        onKeyDown={handleKeyDown}
        placeholder={adding ? "Add to your request…" : placeholder}
        className="min-h-9 max-h-40 flex-1 resize-none py-1.5"
      />
      {isStreaming ? (
        <Button
          type="button"
          variant="outline"
          size="icon"
          onClick={onStop}
          aria-label="Stop response"
          data-testid="chat-stop"
        >
          <Square className="w-4 h-4" aria-hidden="true" />
        </Button>
      ) : (
        <span className="flex" title={blocked ? sendBlockedReason : undefined}>
          <Button
            type="submit"
            size="icon"
            disabled={!input.trim() || blocked}
            aria-label={adding ? "Add to request" : "Send message"}
            data-testid={adding ? "chat-add-to-request" : "chat-send"}
          >
            {adding ? (
              <Plus className="w-4 h-4" aria-hidden="true" />
            ) : (
              <Send className="w-4 h-4" aria-hidden="true" />
            )}
          </Button>
        </span>
      )}
    </form>
  )
}
