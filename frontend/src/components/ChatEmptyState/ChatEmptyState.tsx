// frontend/src/components/ChatEmptyState/ChatEmptyState.tsx
import { useState } from "react"
import { ArrowUp, ArrowRight } from "lucide-react"
import { useAppStore } from "@/store/store"
import { Button } from "@/components/ui/button"
import { Textarea } from "@/components/ui/textarea"
import { formatRelativeTime } from "@/lib/relativeTime"
import { SLASH_COMMANDS, resolveSlashCommand } from "@/components/ChatPanel/slashCommands"
import type { SlashCommand } from "@/components/ChatPanel/slashCommands"
import { SlashCommandMenu } from "@/components/ChatPanel/SlashCommandMenu"
import { getStarterQuestions } from "./starterQuestions"

interface ChatEmptyStateProps {
  input: string
  setInput: (value: string) => void
  onSend: (text: string) => void
  disabled?: boolean
}

export function ChatEmptyState({
  input,
  setInput,
  onSend,
  disabled = false,
}: ChatEmptyStateProps) {
  const workspace = useAppStore((s) =>
    s.domains.find((d) => d.id === s.activeDomainId),
  )
  const [slashMenuIndex, setSlashMenuIndex] = useState(0)

  const provider = workspace?.tenants[0]?.provider
  const starters = getStarterQuestions(provider)
  const lastSyncedAt = workspace?.last_synced_at ?? null

  // Slash command menu state — mirrors ChatPanel's active-thread input so
  // slash commands work identically here.
  const showSlashMenu =
    !disabled && input.startsWith("/") && !input.slice(1).includes(" ")
  const slashQuery = showSlashMenu ? input.slice(1) : ""
  const filteredCommands = SLASH_COMMANDS.filter((cmd) =>
    cmd.name.startsWith(slashQuery),
  )

  function selectSlashCommand(cmd: SlashCommand) {
    setInput(`/${cmd.name} `)
    setSlashMenuIndex(0)
  }

  function submit(text: string) {
    const trimmed = text.trim()
    if (!trimmed || disabled) return
    setInput("")
    onSend(resolveSlashCommand(trimmed))
  }

  function handleSubmit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault()
    submit(input)
  }

  function handleKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (showSlashMenu && filteredCommands.length > 0) {
      if (e.key === "ArrowDown") {
        e.preventDefault()
        setSlashMenuIndex((i) => (i + 1) % filteredCommands.length)
        return
      }
      if (e.key === "ArrowUp") {
        e.preventDefault()
        setSlashMenuIndex(
          (i) => (i - 1 + filteredCommands.length) % filteredCommands.length,
        )
        return
      }
      if (e.key === "Tab" || e.key === "Enter") {
        e.preventDefault()
        selectSlashCommand(filteredCommands[slashMenuIndex])
        return
      }
    }

    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault()
      submit(input)
    }
  }

  return (
    <div
      className="flex h-full flex-col items-center justify-center px-6 py-10"
      data-testid="chat-empty-state"
    >
      <div className="w-full max-w-3xl">
        <h1 className="text-center text-2xl font-medium leading-snug md:text-3xl">
          I&apos;m Scout! Your AI-powered Data Analyst.
          <br />
          How can I assist you today?
        </h1>

        <div className="mt-10 grid grid-cols-1 gap-3 md:grid-cols-3">
          {starters.map((question, idx) => (
            <button
              key={idx}
              type="button"
              onClick={() => submit(question)}
              disabled={disabled}
              className="group flex items-start justify-between gap-2 rounded-lg border bg-card p-4 text-left text-sm transition hover:bg-accent disabled:opacity-50"
              data-testid={`starter-question-${idx}`}
            >
              <span>{question}</span>
              <ArrowRight
                className="h-4 w-4 shrink-0 text-muted-foreground transition group-hover:text-foreground"
                aria-hidden
              />
            </button>
          ))}
        </div>

        <form onSubmit={handleSubmit} className="relative mt-10">
          <SlashCommandMenu
            query={slashQuery}
            onSelect={selectSlashCommand}
            visible={showSlashMenu}
            selectedIndex={slashMenuIndex}
          />
          <Textarea
            data-testid="chat-input-prominent"
            value={input}
            onChange={(e) => {
              setInput(e.target.value)
              setSlashMenuIndex(0)
            }}
            onKeyDown={handleKeyDown}
            placeholder="Ask about your data..."
            disabled={disabled}
            rows={1}
            className="min-h-0 resize-none rounded-xl border bg-background px-4 py-3 pr-14 text-base shadow-sm"
          />
          <Button
            type="submit"
            size="icon"
            disabled={disabled || !input.trim()}
            className="absolute bottom-3 right-3"
          >
            <ArrowUp className="h-4 w-4" />
          </Button>
        </form>

        <p
          className="mt-3 text-center text-xs text-muted-foreground"
          data-testid="data-freshness"
        >
          Scout can only read your data — never modify or delete it.
          {lastSyncedAt && (
            <> Data last synced {formatRelativeTime(lastSyncedAt)}.</>
          )}
        </p>
      </div>
    </div>
  )
}
