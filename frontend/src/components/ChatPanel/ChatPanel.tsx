import { useChat } from "@ai-sdk/react"
import { DefaultChatTransport, type UIMessage } from "ai"
import { useCallback, useEffect, useRef, useState } from "react"
import { getCsrfToken, api, ApiError } from "@/api/client"
import { BASE_PATH } from "@/config"
import { useAppStore } from "@/store/store"
import { ChatMessage } from "@/components/ChatMessage/ChatMessage"
import { MaterializationProgressBanner } from "@/components/MaterializationStatus/MaterializationProgressBanner"
import { useWorkspaceJobs } from "@/contexts/WorkspaceJobsContext"
import { ChatEmptyState } from "@/components/ChatEmptyState"
import { ChatComposer } from "./ChatComposer"
import { ChatCanvasPanel } from "./ChatCanvasPanel"
import { ChatThreadHeader, type ThreadPanelMode } from "./ChatThreadHeader"
import {
  ChatThreadSidePanel,
  type ThreadArtifactSummary,
} from "./ChatThreadSidePanel"
import {
  ChatErrorNotice,
  ChatOverloadNotice,
  ChatThinkingIndicator,
} from "./ChatStatus"
import { writeSavedThreadId, clearSavedThreadId } from "./threadStorage"
import { decideOverloadAction, isRetryableErrorPart } from "./overloadRetry"

export function ChatPanel() {
  const activeDomainId = useAppStore((s) => s.activeDomainId)
  const threadId = useAppStore((s) => s.threadId)
  const threads = useAppStore((s) => s.threads)
  const fetchThreads = useAppStore((s) => s.uiActions.fetchThreads)
  const updateThreadTitle = useAppStore((s) => s.uiActions.updateThreadTitle)
  const newThread = useAppStore((s) => s.uiActions.newThread)
  const openArtifact = useAppStore((s) => s.uiActions.openArtifact)
  const scrollRef = useRef<HTMLDivElement>(null)
  const [input, setInput] = useState("")
  const [messageReloadKey, setMessageReloadKey] = useState(0)
  const [threadPanelOpen, setThreadPanelOpen] = useState(false)
  const [threadPanelMode, setThreadPanelMode] = useState<ThreadPanelMode>("files")
  const [threadArtifacts, setThreadArtifacts] = useState<ThreadArtifactSummary[]>([])
  const [threadArtifactsStatus, setThreadArtifactsStatus] =
    useState<"idle" | "loading" | "loaded" | "error">("idle")
  const [threadArtifactsError, setThreadArtifactsError] = useState<string | null>(null)
  const prevStatusRef = useRef<string>("")
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
  const currentThread = threads.find((thread) => thread.id === threadId)
  const threadTitle = currentThread?.title ?? "Untitled"
  const titleIsCustom = currentThread?.title_is_custom ?? false

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

  const loadThreadArtifacts = useCallback(async () => {
    if (!activeDomainId || !threadId) return
    setThreadArtifactsStatus("loading")
    setThreadArtifactsError(null)
    try {
      const response = await api.get<{ results: ThreadArtifactSummary[] }>(
        `/api/workspaces/${activeDomainId}/threads/${threadId}/artifacts/`,
      )
      setThreadArtifacts(response.results)
      setThreadArtifactsStatus("loaded")
    } catch (loadError) {
      setThreadArtifactsStatus("error")
      setThreadArtifactsError(
        loadError instanceof Error ? loadError.message : "Failed to load files",
      )
    }
  }, [activeDomainId, threadId])

  function openThreadFiles() {
    if (threadPanelOpen && threadPanelMode === "files") {
      setThreadPanelOpen(false)
      return
    }
    setThreadPanelMode("files")
    setThreadPanelOpen(true)
  }

  function openThreadCanvas() {
    if (threadPanelOpen && threadPanelMode === "canvas") {
      setThreadPanelOpen(false)
      return
    }
    setThreadPanelMode("canvas")
    setThreadPanelOpen(true)
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

  useEffect(() => {
    setThreadPanelOpen(false)
    setThreadArtifacts([])
    setThreadArtifactsStatus("idle")
    setThreadArtifactsError(null)
  }, [threadId])

  useEffect(() => {
    if (messages.length === 0) {
      setThreadPanelOpen(false)
    }
  }, [messages.length])

  useEffect(() => {
    if (threadPanelOpen && threadPanelMode === "files") {
      void loadThreadArtifacts()
    }
  }, [threadPanelOpen, threadPanelMode, loadThreadArtifacts])

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
      if (threadPanelOpen && threadPanelMode === "files") {
        void loadThreadArtifacts()
      }
    }
    prevStatusRef.current = status
  }, [
    status,
    activeDomainId,
    fetchThreads,
    loadThreadArtifacts,
    threadPanelMode,
    threadPanelOpen,
  ])

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

  function handleSend(text: string) {
    resetOverloadState()
    sendMessage({ text })
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

  async function handleTitleChange(title: string) {
    if (!activeDomainId || !threadId) return
    await updateThreadTitle(threadId, title, activeDomainId)
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
      <div className="h-full min-w-0">
        <ChatEmptyState
          input={input}
          setInput={setInput}
          onSend={handleSend}
          disabled={isStreaming}
        />
      </div>
    )
  }

  return (
    <div className="flex h-full min-w-0">
      <div className="flex min-w-0 flex-1 flex-col">
        <ChatThreadHeader
          title={threadTitle}
          titleIsCustom={titleIsCustom}
          panelOpen={threadPanelOpen}
          panelMode={threadPanelMode}
          onTitleChange={handleTitleChange}
          onOpenFiles={openThreadFiles}
          onOpenCanvas={openThreadCanvas}
        />
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
          {isStreaming && <ChatThinkingIndicator />}
          {error && <ChatErrorNotice error={error} onStartNewThread={startFreshThread} />}
          {overloadNotice && <ChatOverloadNotice onRetry={handleOverloadRetry} />}
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
          <ChatComposer
            input={input}
            setInput={setInput}
            onSend={handleSend}
            isStreaming={isStreaming}
            onStop={() => stop()}
          />
        </div>
      </div>
      <ChatThreadSidePanel
        open={threadPanelOpen}
        mode={threadPanelMode}
        artifacts={threadArtifacts}
        filesStatus={threadArtifactsStatus}
        filesError={threadArtifactsError}
        onClose={() => setThreadPanelOpen(false)}
        onOpenArtifact={openArtifact}
        onRefreshFiles={loadThreadArtifacts}
        canvas={<ChatCanvasPanel workspaceId={activeDomainId} />}
      />
    </div>
  )
}
