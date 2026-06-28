import { useChat } from "@ai-sdk/react"
import { DefaultChatTransport, type UIMessage } from "ai"
import { useEffect, useRef, useState } from "react"
import { getCsrfToken, api, ApiError } from "@/api/client"
import { BASE_PATH } from "@/config"
import { useAppStore } from "@/store/store"
import { ChatMessage } from "@/components/ChatMessage/ChatMessage"
import { MaterializationProgressBanner } from "@/components/MaterializationStatus/MaterializationProgressBanner"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Send, Square } from "lucide-react"
import { SLASH_COMMANDS, resolveSlashCommand } from "./slashCommands"
import { SlashCommandMenu } from "./SlashCommandMenu"
import { useWorkspaceJobs } from "@/contexts/WorkspaceJobsContext"
import { ChatEmptyState } from "@/components/ChatEmptyState"
import { writeSavedThreadId, clearSavedThreadId } from "./threadStorage"
import { decideOverloadAction, isRetryableErrorPart } from "./overloadRetry"

/** True when an error looks like a stale/missing-thread rejection (HTTP 404 or
 * a body containing the backend's "Thread not found" marker). */
function isStaleThreadError(error: Error | undefined): boolean {
  if (!error) return false
  if (error instanceof ApiError && error.status === 404) return true
  return error.message.includes("Thread not found")
}

export function ChatPanel() {
  const activeDomainId = useAppStore((s) => s.activeDomainId)
  const threadId = useAppStore((s) => s.threadId)
  const fetchThreads = useAppStore((s) => s.uiActions.fetchThreads)
  const newThread = useAppStore((s) => s.uiActions.newThread)
  const pendingChatInput = useAppStore((s) => s.pendingChatInput)
  const setPendingChatInput = useAppStore((s) => s.uiActions.setPendingChatInput)
  const scrollRef = useRef<HTMLDivElement>(null)
  const [input, setInput] = useState("")
  const [slashMenuIndex, setSlashMenuIndex] = useState(0)
  const [messageReloadKey, setMessageReloadKey] = useState(0)
  const prevStatusRef = useRef<string>("")
  // Pull text pushed from another surface (e.g. an "Edit definition" lineage button) into
  // the composer, then clear it so it isn't re-applied on the next render.
  useEffect(() => {
    if (pendingChatInput != null) {
      setInput(pendingChatInput)
      setPendingChatInput(null)
    }
  }, [pendingChatInput, setPendingChatInput])
  // Transient-overload auto-retry (see ./overloadRetry):
  //   hitRetryableRef — a retryable-error data part arrived during this turn
  //   retriedRef      — we've already auto-retried this turn once
  const hitRetryableRef = useRef(false)
  const retriedRef = useRef(false)
  const prevRetryStatusRef = useRef<string>("")
  const [overloadNotice, setOverloadNotice] = useState(false)

  const {
    jobsByThreadId,
    recentlyCompletedThreadIds,
    recentTerminationsByToolCallId,
    notifyJobLikelyStarted,
  } = useWorkspaceJobs()
  const activeMaterializationJob = jobsByThreadId[threadId] ?? null

  // Use a ref so the transport body closure always reads fresh values,
  // even though useChat caches the transport from the first render.
  const contextRef = useRef({ workspaceId: activeDomainId, threadId })
  contextRef.current = { workspaceId: activeDomainId, threadId }

  const [transport] = useState(
    () =>
      new DefaultChatTransport({
        api: `${BASE_PATH}/api/chat/`,
        credentials: "include",
        headers: () => ({ "X-CSRFToken": getCsrfToken() }),
        body: () => ({ data: contextRef.current }),
      }),
  )

  const { messages, sendMessage, status, stop, error, setMessages, regenerate } = useChat({
    transport,
    onData: (part) => {
      if (isRetryableErrorPart(part)) hitRetryableRef.current = true
    },
  })

  // Clear auto-retry bookkeeping at the start of each user-initiated turn.
  function resetOverloadState() {
    hitRetryableRef.current = false
    retriedRef.current = false
    setOverloadNotice(false)
  }

  const isStreaming = status === "streaming" || status === "submitted"

  // Slash command menu state
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

  // Load messages from backend when threadId changes (or after a background job
  // completes). On success — including an empty array for a brand-new thread —
  // persist this (workspace, thread) pair so a later bare /chat visit can
  // restore it. We persist ONLY here so a stale/foreign thread (which 404s
  // below) never gets stamped into this workspace's localStorage. A 404 means
  // the thread exists but isn't ours / isn't this workspace's: drop the saved
  // id and start a fresh thread so the user lands in a clean chat instead of a
  // haunted one.
  useEffect(() => {
    if (!threadId || !activeDomainId) return
    let cancelled = false

    async function loadMessages() {
      try {
        const msgs = await api.get<UIMessage[]>(
          `/api/workspaces/${activeDomainId}/threads/${threadId}/messages/`,
        )
        if (cancelled) return
        setMessages(msgs)
        if (activeDomainId && threadId) {
          writeSavedThreadId(activeDomainId, threadId)
        }
      } catch (err) {
        if (cancelled) return
        if (err instanceof ApiError && err.status === 404) {
          // Stale / cross-workspace thread: recover into a fresh chat.
          if (activeDomainId) clearSavedThreadId(activeDomainId, threadId)
          setMessages([])
          newThread()
          return
        }
        // New thread or transient fetch failure — start with empty.
        setMessages([])
      }
    }

    loadMessages()
    return () => { cancelled = true }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [threadId, activeDomainId, messageReloadKey])

  // Reload messages when a background materialization job for this thread
  // completes — but NOT while we're streaming a new turn. A mid-stream reload
  // would tear down the in-flight messages array and lose the user's current
  // tokens. The Thread.updated_at bump from the resume task triggers the
  // sidebar refetch, so the user still sees the green-dot indicator and can
  // click into the thread to get the new agent message on a fresh load.
  useEffect(() => {
    if (isStreaming) return
    if (threadId && recentlyCompletedThreadIds.includes(threadId)) {
      setMessageReloadKey((k) => k + 1)
    }
  }, [threadId, recentlyCompletedThreadIds, isStreaming])

  // Refresh thread list when streaming finishes (so new threads appear)
  useEffect(() => {
    if (prevStatusRef.current === "streaming" && status === "ready" && activeDomainId) {
      fetchThreads(activeDomainId)
    }
    prevStatusRef.current = status
  }, [status, activeDomainId, fetchThreads])

  // Auto-retry a turn once if it hit a transient Anthropic overload; if the
  // retry also hits it, surface a notice instead. See ./overloadRetry.
  useEffect(() => {
    const prev = prevRetryStatusRef.current
    prevRetryStatusRef.current = status
    const justFinished =
      (prev === "streaming" || prev === "submitted") && status === "ready"
    if (!justFinished) return

    const action = decideOverloadAction({
      hitRetryable: hitRetryableRef.current,
      alreadyRetried: retriedRef.current,
    })
    hitRetryableRef.current = false
    if (action === "retry") {
      retriedRef.current = true
      void regenerate()
    } else if (action === "notify") {
      retriedRef.current = false
      setOverloadNotice(true)
    }
  }, [status, regenerate])

  // Auto-scroll to bottom on new messages
  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight
    }
  }, [messages])

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    const text = input.trim()
    if (!text || isStreaming) return

    setInput("")
    resetOverloadState()
    sendMessage({ text: resolveSlashCommand(text) })
  }

  function handleOverloadRetry() {
    resetOverloadState()
    void regenerate()
  }

  // Recover from a stale/unavailable thread: forget the saved id for this
  // workspace and start a fresh thread. The URL sync hook then rewrites the
  // address bar to the new thread.
  function startFreshThread() {
    if (activeDomainId) clearSavedThreadId(activeDomainId)
    setMessages([])
    newThread()
  }

  function handleKeyDown(e: React.KeyboardEvent) {
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

  if (!activeDomainId) {
    return (
      <div className="flex-1 flex items-center justify-center text-muted-foreground">
        Select a domain to start chatting
      </div>
    )
  }

  if (messages.length === 0) {
    return (
      <ChatEmptyState
        input={input}
        setInput={setInput}
        onSend={(text) => {
          resetOverloadState()
          sendMessage({ text })
        }}
        disabled={isStreaming}
      />
    )
  }

  return (
    <div className="flex flex-col h-full">
      {/* Message list */}
      <div ref={scrollRef} className="flex-1 overflow-y-auto p-4 space-y-4">
        {messages.map((msg: UIMessage, msgIdx: number) => (
          <ChatMessage
            key={msg.id}
            message={msg}
            isActiveMessage={isStreaming && msgIdx === messages.length - 1}
            workspaceId={activeDomainId ?? undefined}
            threadId={threadId}
            activeMaterializationJob={activeMaterializationJob}
            recentTerminationsByToolCallId={recentTerminationsByToolCallId}
            onRetryDispatched={notifyJobLikelyStarted}
          />
        ))}
        {isStreaming && <ThinkingIndicator />}
        {error && <ChatError error={error} onStartNewThread={startFreshThread} />}
        {overloadNotice && <OverloadNotice onRetry={handleOverloadRetry} />}
      </div>

      {/* Materialization progress banner — always visible when a job is active for this thread */}
      {activeMaterializationJob
        && (activeMaterializationJob.state === "pending" || activeMaterializationJob.state === "running")
        && activeDomainId && (
          <MaterializationProgressBanner
            job={activeMaterializationJob}
            workspaceId={activeDomainId}
          />
        )}

      {/* Input area */}
      <div className="border-t p-4">
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
            placeholder="Ask about your data..."
            disabled={isStreaming}
            className="flex-1"
          />
          {isStreaming ? (
            <Button type="button" variant="outline" size="icon" onClick={() => stop()}>
              <Square className="w-4 h-4" />
            </Button>
          ) : (
            <Button type="submit" size="icon" disabled={!input.trim()}>
              <Send className="w-4 h-4" />
            </Button>
          )}
        </form>
      </div>
    </div>
  )
}

/**
 * Friendly chat error. Never renders a raw response body (e.g. the JSON
 * `{"error":"Thread not found"}` the backend returns). The stale-thread case
 * gets a recovery button; everything else gets a generic message, with the real
 * error kept in the console for debugging.
 */
function ChatError({
  error,
  onStartNewThread,
}: {
  error: Error
  onStartNewThread: () => void
}) {
  const stale = isStaleThreadError(error)
  useEffect(() => {
    console.error("[Scout] Chat error:", error)
  }, [error])

  return (
    <div
      className="text-sm text-destructive bg-destructive/10 rounded-lg px-4 py-3 space-y-2"
      data-testid="chat-error"
    >
      <p>
        {stale
          ? "This conversation is no longer available."
          : "Something went wrong. Please try again."}
      </p>
      {stale && (
        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={onStartNewThread}
          data-testid="chat-error-new-thread"
        >
          Start new chat
        </Button>
      )}
    </div>
  )
}

/**
 * Shown when a turn hit a transient Anthropic overload and the one automatic
 * retry also failed. Offers a manual retry rather than a dead end.
 */
function OverloadNotice({ onRetry }: { onRetry: () => void }) {
  return (
    <div
      className="text-sm text-muted-foreground bg-muted rounded-lg px-4 py-3 space-y-2"
      data-testid="chat-overload-notice"
    >
      <p>The assistant is busy right now. Please try again in a moment.</p>
      <Button
        type="button"
        variant="outline"
        size="sm"
        onClick={onRetry}
        data-testid="chat-overload-retry"
      >
        Retry
      </Button>
    </div>
  )
}

function ThinkingIndicator() {
  return (
    <div className="flex items-start gap-3 py-2" data-testid="thinking-indicator">
      <div className="flex items-center gap-1.5 rounded-lg bg-muted px-4 py-3">
        {[0, 1, 2].map((i) => (
          <span
            key={i}
            className="block h-2 w-2 rounded-full bg-muted-foreground/60"
            style={{
              animation: "thinking-dot 1.4s ease-in-out infinite",
              animationDelay: `${i * 0.2}s`,
            }}
          />
        ))}
      </div>
      <style>{`
        @keyframes thinking-dot {
          0%, 80%, 100% { opacity: 0.3; transform: scale(0.8); }
          40% { opacity: 1; transform: scale(1); }
        }
      `}</style>
    </div>
  )
}
