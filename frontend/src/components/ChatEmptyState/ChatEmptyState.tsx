import { useId, useRef, useState } from "react"
import { ArrowUp, ArrowRight } from "lucide-react"
import { useAppStore } from "@/store/store"
import { Button } from "@/components/ui/button"
import { Textarea } from "@/components/ui/textarea"
import { formatRelativeTime } from "@/lib/relativeTime"
import { workspaceSourcesLabel, workspaceSourcesShortLabel } from "@/lib/workspaceSources"
import { cn } from "@/lib/utils"
import { useWorkspaceRole } from "@/hooks/useWorkspaceRole"
import { matchSlashCommands, resolveSlashCommand } from "@/components/ChatPanel/slashCommands"
import type { SlashCommand } from "@/components/ChatPanel/slashCommands"
import { SlashCommandMenu } from "@/components/ChatPanel/SlashCommandMenu"
import { CharCounter } from "@/components/ChatPanel/CharCounter"
import {
  isComposingKey,
  isOverLimit,
  messageLength,
  useAutoGrow,
} from "@/components/ChatPanel/messageLimit"
import { AttachButton, AttachmentTray } from "@/components/ChatPanel/attachments/AttachmentTray"
import {
  attachPastedFiles,
  attachmentsBlockReason,
  type ChatAttachments,
} from "@/components/ChatPanel/attachments/useChatAttachments"
import { getStarterQuestions } from "./starterQuestions"

// Centres a 36px button on the one-line prompt: 50px tall at text-base, 46px at md:text-sm.
const BUTTON_BOTTOM = "bottom-[7px] md:bottom-[5px]"

interface ChatEmptyPromptProps {
  input: string
  setInput: (value: string) => void
  onSend: (text: string) => void
  disabled?: boolean
  lastSyncedAt?: string | null
  className?: string
  attachments?: ChatAttachments
}

export function ChatEmptyPrompt({
  input,
  setInput,
  onSend,
  disabled = false,
  lastSyncedAt = null,
  className,
  attachments,
}: ChatEmptyPromptProps) {
  const [slashMenuIndex, setSlashMenuIndex] = useState(0)
  const { canWrite } = useWorkspaceRole()
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  useAutoGrow(textareaRef, input)
  const counterId = useId()
  const trimmedInput = input.trim()
  const message = trimmedInput ? resolveSlashCommand(trimmedInput) : ""
  const length = messageLength(message)
  const tooLong = isOverLimit(length)
  const holdReason = (tooLong ? "Message is too long" : null) ?? attachmentsBlockReason(attachments)
  const canSend = Boolean(trimmedInput || attachments?.ready) && !holdReason && !disabled

  // Slash command menu state — mirrors ChatPanel's active-thread input so
  // slash commands work identically here.
  const showSlashMenu = input.startsWith("/") && !/\s/.test(input.slice(1))
  const slashQuery = showSlashMenu ? input.slice(1) : ""
  const filteredCommands = matchSlashCommands(slashQuery, canWrite)

  function selectSlashCommand(cmd: SlashCommand) {
    setInput(`/${cmd.name} `)
    setSlashMenuIndex(0)
  }

  function submit() {
    if (!canSend) return
    setInput("")
    onSend(message)
  }

  function handleSubmit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault()
    submit()
  }

  function handleKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (isComposingKey(e)) return
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
    <div className={cn("w-full", className)}>
      {attachments && (
        <AttachmentTray
          items={attachments.items}
          notice={attachments.notice}
          onRemove={attachments.remove}
        />
      )}
      <form onSubmit={handleSubmit} className={cn("relative", attachments?.items.length && "mt-2")}>
        <SlashCommandMenu
          query={slashQuery}
          onSelect={selectSlashCommand}
          visible={showSlashMenu}
          selectedIndex={slashMenuIndex}
          canWrite={canWrite}
        />
        <Textarea
          ref={textareaRef}
          data-testid="chat-input-prominent"
          aria-invalid={tooLong || undefined}
          aria-describedby={counterId}
          value={input}
          onChange={(e) => {
            setInput(e.target.value)
            setSlashMenuIndex(0)
          }}
          onKeyDown={handleKeyDown}
          onPaste={(e) => attachPastedFiles(e, attachments)}
          placeholder="Ask about your data..."
          rows={1}
          className={cn(
            "field-sizing-fixed min-h-0 resize-none rounded-xl border bg-background px-4 py-3 pr-14 text-base shadow-sm",
            attachments && "pl-12",
          )}
        />
        {attachments && (
          <AttachButton
            onFiles={attachments.add}
            disabled={disabled}
            className={`absolute left-2 ${BUTTON_BOTTOM}`}
          />
        )}
        {/* The title sits on a wrapper: a disabled button shows no tooltip. */}
        <span className={`absolute right-3 flex ${BUTTON_BOTTOM}`} title={holdReason ?? undefined}>
          <Button type="submit" size="icon" disabled={!canSend} aria-label="Send message">
            <ArrowUp className="h-4 w-4" aria-hidden="true" />
          </Button>
        </span>
      </form>
      <CharCounter
        id={counterId}
        length={length}
        suggestAttach={Boolean(attachments)}
        className="mt-1"
      />

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
  )
}

interface ChatEmptyStateProps {
  input: string
  setInput: (value: string) => void
  onSend: (text: string) => void
  disabled?: boolean
  attachments?: ChatAttachments
}

export function ChatEmptyState({
  input,
  setInput,
  onSend,
  disabled = false,
  attachments,
}: ChatEmptyStateProps) {
  const workspace = useAppStore((s) =>
    s.domains.find((d) => d.id === s.activeDomainId),
  )

  const openWorkspaceSwitcher = useAppStore((s) => s.uiActions.openWorkspaceSwitcher)
  const canChangeWorkspace = useAppStore((s) => s.workspaceSwitcherMounts > 0)

  const provider = workspace?.tenants[0]?.provider
  const starters = getStarterQuestions(provider)
  const lastSyncedAt = workspace?.last_synced_at ?? null
  // A starter question goes out with the files attached so far, so it waits for them too.
  const startersHoldReason = attachmentsBlockReason(attachments)
  const startersBlocked = disabled || startersHoldReason !== null

  function submit(text: string) {
    const trimmed = text.trim()
    if (!trimmed || startersBlocked) return
    setInput("")
    onSend(resolveSlashCommand(trimmed))
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
            // The title sits on a wrapper: a disabled button shows no tooltip.
            <span key={idx} className="flex" title={startersHoldReason ?? undefined}>
              <button
                type="button"
                onClick={() => submit(question)}
                disabled={startersBlocked}
                className="group flex w-full items-start justify-between gap-2 rounded-lg border bg-card p-4 text-left text-sm transition hover:bg-accent disabled:opacity-50"
                data-testid={`starter-question-${idx}`}
              >
                <span>{question}</span>
                <ArrowRight
                  className="h-4 w-4 shrink-0 text-muted-foreground transition group-hover:text-foreground"
                  aria-hidden
                />
              </button>
            </span>
          ))}
        </div>

        {workspace && (
          <p
            className="mt-10 text-center text-sm text-muted-foreground"
            data-testid="chat-workspace-context"
          >
            Asking about{" "}
            <span className="font-medium text-foreground">{workspace.display_name}</span>
            {" · "}
            <span title={workspaceSourcesLabel(workspace)} data-testid="chat-workspace-sources">
              {workspace.tenants.length > 0 &&
                `${workspace.tenants.length === 1 ? "Source" : "Sources"}: `}
              {workspaceSourcesShortLabel(workspace)}
            </span>
            {canChangeWorkspace && (
              <>
                {" "}
                <button
                  type="button"
                  onClick={openWorkspaceSwitcher}
                  className="underline underline-offset-2 hover:text-foreground"
                  data-testid="chat-workspace-change"
                >
                  Change
                </button>
              </>
            )}
          </p>
        )}

        <ChatEmptyPrompt
          input={input}
          setInput={setInput}
          onSend={onSend}
          disabled={disabled}
          attachments={attachments}
          lastSyncedAt={lastSyncedAt}
          className={workspace ? "mt-3" : "mt-10"}
        />
      </div>
    </div>
  )
}
