import { useEffect, useId, useRef, useState } from "react"
import type { FormEvent, KeyboardEvent } from "react"
import { Plus, Quote, Send, Square, X } from "lucide-react"

import { Button } from "@/components/ui/button"
import { Textarea } from "@/components/ui/textarea"
import { useWorkspaceRole } from "@/hooks/useWorkspaceRole"
import { composeQuotedMessage } from "@/components/ChatMessage/quotes"
import { PART_SEPARATOR } from "@/api/pendingRequests"
import { matchSlashCommands, resolveSlashCommand, type SlashCommand } from "./slashCommands"
import { SlashCommandMenu } from "./SlashCommandMenu"
import { CharCounter } from "./CharCounter"
import { isComposingKey, isOverLimit, messageLength, useAutoGrow } from "./messageLimit"
import { AttachButton, AttachmentTray } from "./attachments/AttachmentTray"
import {
  attachPastedFiles,
  attachmentsBlockReason,
  type ChatAttachments,
} from "./attachments/useChatAttachments"

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
  /** A held request this message is joined onto; the server's limit covers both. */
  heldText?: string
  /** Files attached to the next message; without it the composer takes text only. */
  attachments?: ChatAttachments
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
  heldText,
  attachments,
}: ChatComposerProps) {
  const adding = mode === "add"
  const blocked = sendBlocked && !adding
  const [slashMenuIndex, setSlashMenuIndex] = useState(0)
  const { canWrite } = useWorkspaceRole()
  const inputRef = useRef<HTMLTextAreaElement>(null)
  useAutoGrow(inputRef, input)

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

  const counterId = useId()
  const trimmed = input.trim()
  // What goes out, and what the server measures: quotes and an expanded slash command
  // included, joined onto a held request's text when there is one.
  const message = trimmed ? composeQuotedMessage(quotes, resolveSlashCommand(trimmed)) : ""
  const measured = message && heldText ? `${heldText}${PART_SEPARATOR}${message}` : message
  const length = messageLength(measured)
  const tooLong = isOverLimit(length)
  const hasFiles = attachments?.ready ?? false
  // A held request takes text only: its files would have nowhere to go.
  const addBlock =
    adding && attachments?.items.length ? "Files can be sent once the data has loaded" : null
  const holdReason =
    (blocked ? sendBlockedReason : null)
    ?? (tooLong ? "Message is too long" : null)
    // Ahead of the upload state: waiting for an upload would not help here.
    ?? addBlock
    ?? attachmentsBlockReason(attachments)
    // Quotes are only read back as quotes when a reply follows them.
    ?? (quotes.length && !trimmed ? "Type a reply to go with the quotes" : null)
  const canSend = Boolean(trimmed || hasFiles) && !holdReason

  function submit() {
    if (!canSend || isStreaming) return
    setInput("")
    if (quotes.length) onQuotesSent?.()
    onSend(message)
  }

  function handleSubmit(e: FormEvent) {
    e.preventDefault()
    submit()
  }

  function handleKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (isComposingKey(e)) return
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
    <div className="flex flex-col gap-2">
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
      {attachments && (
        <AttachmentTray
          items={attachments.items}
          notice={attachments.notice}
          onRemove={attachments.remove}
        />
      )}
      <form onSubmit={handleSubmit} className="relative flex items-end gap-2">
        <SlashCommandMenu
          query={slashQuery}
          onSelect={selectSlashCommand}
          visible={showSlashMenu}
          selectedIndex={slashMenuIndex}
          canWrite={canWrite}
        />
        {attachments && !adding && <AttachButton onFiles={attachments.add} className="-mr-1" />}
        <Textarea
          ref={inputRef}
          data-testid="chat-input"
          rows={1}
          aria-invalid={tooLong || undefined}
          aria-describedby={counterId}
          value={input}
          onChange={(e) => {
            setInput(e.target.value)
            setSlashMenuIndex(0)
          }}
          onKeyDown={handleKeyDown}
          onPaste={(e) => attachPastedFiles(e, attachments)}
          placeholder={adding ? "Add to your request…" : placeholder}
          className="field-sizing-fixed min-h-9 flex-1 resize-none py-1.5"
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
          <span
            className="flex items-end"
            title={holdReason ?? undefined}
          >
            <Button
              type="submit"
              size="icon"
              disabled={!canSend}
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
      <CharCounter id={counterId} length={length} includesHeld={Boolean(message && heldText)}
        suggestAttach={Boolean(attachments) && !adding}
      />
    </div>
  )
}
