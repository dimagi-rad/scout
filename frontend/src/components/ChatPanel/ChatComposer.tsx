import { useEffect, useRef, useState } from "react"
import type { FormEvent, KeyboardEvent } from "react"
import { Plus, Quote, Send, Square, X } from "lucide-react"

import { Button } from "@/components/ui/button"
import { Textarea } from "@/components/ui/textarea"
import { useWorkspaceRole } from "@/hooks/useWorkspaceRole"
import { composeQuotedMessage } from "@/components/ChatMessage/quotes"
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
  /** Passages of a Scout reply this message answers; sent ahead of the text as blockquotes. */
  quotes?: string[]
  onRemoveQuote?: (index: number) => void
  /** Called as a send takes the quotes, so the next message starts without them. */
  onQuotesSent?: () => void
  /** Bumped when a quote is picked, to move focus to the input for the reply. */
  focusRequest?: number
}

const NO_QUOTES: string[] = []

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
  quotes = NO_QUOTES,
  onRemoveQuote,
  onQuotesSent,
  focusRequest = 0,
}: ChatComposerProps) {
  const adding = mode === "add"
  const blocked = sendBlocked && !adding
  const [slashMenuIndex, setSlashMenuIndex] = useState(0)
  const { canWrite } = useWorkspaceRole()
  const inputRef = useRef<HTMLTextAreaElement>(null)

  // Latched so a remount (e.g. back from the empty state) doesn't refocus for an old pick.
  const handledFocusRef = useRef(focusRequest)

  useEffect(() => {
    if (focusRequest === handledFocusRef.current) return
    handledFocusRef.current = focusRequest
    inputRef.current?.focus()
  }, [focusRequest])

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
    const message = composeQuotedMessage(quotes, resolveSlashCommand(text))
    if (quotes.length) onQuotesSent?.()
    onSend(message)
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
    <div className="space-y-2">
      {quotes.length > 0 && (
        <ul className="space-y-1" aria-label="Quoted text">
          {quotes.map((quote, i) => (
            <li
              key={`${i}:${quote}`}
              className="flex items-start gap-2 rounded-md border-l-2 border-primary bg-muted px-2 py-1 text-xs text-muted-foreground"
              data-testid="composer-quote-chip"
            >
              <Quote className="mt-0.5 h-3 w-3 shrink-0" aria-hidden="true" />
              <span className="line-clamp-2 flex-1 whitespace-pre-wrap">{quote}</span>
              {onRemoveQuote && (
                <button
                  type="button"
                  onClick={() => onRemoveQuote(i)}
                  aria-label={`Remove quote: ${quote.slice(0, 40)}`}
                  className="shrink-0 rounded hover:text-foreground"
                  data-testid="composer-quote-remove"
                >
                  <X className="h-3 w-3" aria-hidden="true" />
                </button>
              )}
            </li>
          ))}
        </ul>
      )}
      <form onSubmit={handleSubmit} className="relative flex gap-2">
        <SlashCommandMenu
          query={slashQuery}
          onSelect={selectSlashCommand}
          visible={showSlashMenu}
          selectedIndex={slashMenuIndex}
          canWrite={canWrite}
        />
        <Textarea
          ref={inputRef}
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
    </div>
  )
}
