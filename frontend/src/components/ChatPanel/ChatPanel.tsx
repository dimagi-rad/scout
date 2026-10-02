import { useChat } from "@ai-sdk/react"
import { DefaultChatTransport, type UIMessage } from "ai"
import { useCallback, useEffect, useRef, useState } from "react"
import { useLocation } from "react-router-dom"
import { getCsrfToken, api, ApiError } from "@/api/client"
import { BASE_PATH } from "@/config"
import { useAppStore } from "@/store/store"
import { ChatMessage } from "@/components/ChatMessage/ChatMessage"
import { workspaceApi } from "@/api/workspaces"
import { SourceFreshness } from "@/components/SourceFreshness"
import { StaleDataBanner } from "@/components/StaleDataBanner"
import { useThreadDraft } from "@/hooks/useThreadDraft"
import { useRefetchOnLoadEnd } from "@/hooks/useRefetchOnLoadEnd"
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
  ChatBusyNotice,
  ChatErrorNotice,
  ChatOverloadNotice,
  ChatStoppedNotice,
  ChatThinkingIndicator,
} from "./ChatStatus"
import { writeSavedThreadId, clearSavedThreadId } from "./threadStorage"
import {
  busyRetryAfter,
  decideOverloadAction,
  isBusyChatError,
  isRetryableErrorPart,
} from "./overloadRetry"
import {
  BUSY_MAX_AUTO_RETRIES,
  BUSY_RETRY_AFTER_SECONDS,
  busyRetryDelayMs,
  busyTracker,
} from "@/api/busy"

export function ChatPanel() {
  const activeDomainId = useAppStore((s) => s.activeDomainId)
  const threadId = useAppStore((s) => s.threadId)
  const threads = useAppStore((s) => s.threads)
  const fetchThreads = useAppStore((s) => s.uiActions.fetchThreads)
  const updateThreadTitle = useAppStore((s) => s.uiActions.updateThreadTitle)
  const newThread = useAppStore((s) => s.uiActions.newThread)
  const openArtifact = useAppStore((s) => s.uiActions.openArtifact)
  const scrollRef = useRef<HTMLDivElement>(null)
  const userId = useAppStore((s) => s.user?.id ?? null)
  const [input, setInput] = useThreadDraft(userId, activeDomainId, threadId)
  const [messageReloadKey, setMessageReloadKey] = useState(0)
  const [threadPanelOpen, setThreadPanelOpen] = useState(false)
  const [threadPanelMode, setThreadPanelMode] = useState<ThreadPanelMode>("files")
  const [threadArtifacts, setThreadArtifacts] = useState<ThreadArtifactSummary[]>([])
  const [threadArtifactsStatus, setThreadArtifactsStatus] =
    useState<"idle" | "loading" | "loaded" | "error">("idle")
  const [threadArtifactsError, setThreadArtifactsError] = useState<string | null>(null)
  const threadArtifactsRequestRef = useRef(0)
  const prevStatusRef = useRef<string>("")
  // Transient-overload auto-retry bookkeeping; see ./overloadRetry.
  const hitRetryableRef = useRef(false)
  const retriedRef = useRef(false)
  const prevRetryStatusRef = useRef<string>("")
  const [overloadNotice, setOverloadNotice] = useState(false)
  // Connection-limit "busy" turns; the shared BusyNotice shows their progress.
  const busyHitRef = useRef<{ retryAfter: number | null } | null>(null)
  const busyAttemptsRef = useRef(0)
  const busyTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const [busyToken] = useState(() => Symbol("chat-busy"))
  const [busyNotice, setBusyNotice] = useState(false)
  const [stoppedNotice, setStoppedNotice] = useState(false)

  const {
    jobsByThreadId,
    workspaceLoads,
    recentlyCompletedThreadIds,
    recentTerminationsByToolCallId,
    notifyJobLikelyStarted,
  } = useWorkspaceJobs()
  const activeMaterializationJob = jobsByThreadId[threadId] ?? null
  // A load the caller has no job for here (a teammate's, or a refresh) still
  // changes the data they are reading, so show it read-only.
  const foreignLoads = (workspaceLoads ?? []).filter(
    (load) =>
      !(
        activeMaterializationJob &&
        load.tenant_name === activeMaterializationJob.tenant_name &&
        load.source_index === activeMaterializationJob.source_index
      ),
  )
  const loadBanners =
    activeDomainId &&
    foreignLoads.map((load) => (
      <MaterializationProgressBanner
        key={`${load.tenant_id}-${load.started_at}`}
        load={load}
        workspaceId={activeDomainId}
      />
    ))
  const workspaceLoading =
    Boolean(activeMaterializationJob) || (workspaceLoads ?? []).length > 0
  // Fetched here, not in the banner and the data-as-of lines, so both read one
  // response and it survives the switch between the empty and thread layouts.
  const [freshness, refetchFreshness] = useRefetchOnLoadEnd(
    workspaceApi.getFreshness,
    activeDomainId,
    workspaceLoading,
  )
  // A refresh queues a job the load poll cannot see until a worker starts it;
  // the freshness endpoint does, so the banner hides instead of re-offering Refresh.
  const handleRefreshStarted = useCallback(() => {
    notifyJobLikelyStarted()
    refetchFreshness()
  }, [notifyJobLikelyStarted, refetchFreshness])
  const staleBanner = activeDomainId && (
    <StaleDataBanner
      key={activeDomainId}
      workspaceId={activeDomainId}
      freshness={freshness}
      loading={workspaceLoading}
      onRefreshStarted={handleRefreshStarted}
    />
  )
  const currentThread = threads.find((thread) => thread.id === threadId)
  const threadTitle = currentThread?.title ?? ""

  // Use a ref so the transport body closure always reads fresh values,
  // even though useChat caches the transport from the first render.
  const contextRef = useRef({ workspaceId: activeDomainId, threadId })
  contextRef.current = { workspaceId: activeDomainId, threadId }
  // The thread whose turn useChat is running. A switch does not abort it, so its
  // outcome can land while another thread is shown.
  const turnThreadRef = useRef<string | null>(null)
  const pathPrefix = useLocation().pathname.startsWith("/embed") ? "/embed" : ""

  const [transport] = useState(
    () =>
      new DefaultChatTransport({
        api: `${BASE_PATH}/api/chat/`,
        credentials: "include",
        headers: () => ({ "X-CSRFToken": getCsrfToken() }),
        body: () => ({ data: contextRef.current }),
      }),
  )

  const {
    messages, sendMessage, status, stop, error, setMessages, regenerate, clearError,
  } = useChat({
    transport,
    onData: (part) => {
      const retryAfter = busyRetryAfter(part)
      if (retryAfter !== undefined) busyHitRef.current = { retryAfter }
      else if (isRetryableErrorPart(part)) hitRetryableRef.current = true
    },
  })
  const busyError = error !== undefined && isBusyChatError(error)

  const cancelBusyRetry = useCallback(() => {
    if (busyTimerRef.current) clearTimeout(busyTimerRef.current)
    busyTimerRef.current = null
    busyHitRef.current = null
    busyAttemptsRef.current = 0
    busyTracker.settle(busyToken)
  }, [busyToken])

  function resetOverloadState() {
    hitRetryableRef.current = false
    retriedRef.current = false
    setOverloadNotice(false)
    setBusyNotice(false)
    cancelBusyRetry()
  }

  // A pending busy retry, or a notice whose Retry would regenerate, belongs to this
  // thread; never replay it into another. threadId is the trigger: this cleanup runs
  // on every thread change, so the dependency must stay even though it isn't read.
  // setMessages does not clear useChat's error, so drop the error notice explicitly.
  useEffect(() => () => {
    cancelBusyRetry()
    setBusyNotice(false)
    setOverloadNotice(false)
    clearError()
  }, [threadId, cancelBusyRetry, clearError])

  const isStreaming = status === "streaming" || status === "submitted"

  const loadThreadArtifacts = useCallback(async () => {
    if (!activeDomainId || !threadId) return
    const requestId = ++threadArtifactsRequestRef.current
    const isCurrentRequest = () =>
      requestId === threadArtifactsRequestRef.current
      && contextRef.current.workspaceId === activeDomainId
      && contextRef.current.threadId === threadId
    setThreadArtifactsStatus("loading")
    setThreadArtifactsError(null)
    try {
      const response = await api.get<{ results: ThreadArtifactSummary[] }>(
        `/api/workspaces/${activeDomainId}/threads/${threadId}/artifacts/`,
      )
      if (!isCurrentRequest()) return
      setThreadArtifacts(response.results)
      setThreadArtifactsStatus("loaded")
    } catch (loadError) {
      if (!isCurrentRequest()) return
      setThreadArtifactsStatus("error")
      setThreadArtifactsError(
        loadError instanceof Error ? loadError.message : "Failed to load artifacts",
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

  // Persist the (workspace, thread) pair ONLY on a successful load, so a
  // stale/foreign thread (which 404s below) never gets stamped into this
  // workspace's localStorage; a 404 instead drops the saved id and starts fresh.
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
    threadArtifactsRequestRef.current += 1
    setThreadPanelOpen(false)
    setThreadArtifacts([])
    setThreadArtifactsStatus("idle")
    setThreadArtifactsError(null)
    setStoppedNotice(false)
    return () => {
      threadArtifactsRequestRef.current += 1
    }
  }, [activeDomainId, threadId])

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

  // Refresh thread list when streaming finishes so new threads appear.
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
    const wasRunning = prev === "streaming" || prev === "submitted"
    // "error" counts only for a busy 503; a hard failure after an overload part must
    // keep its error notice, not be silently re-posted.
    // submitted -> streaming is mid-run; acting on it would drop a busy part that
    // arrived before the first streaming render.
    if (!wasRunning || (status !== "ready" && status !== "error")) return
    // Any finished run releases this thread's "retrying" slot, hard errors included.
    busyTracker.settle(busyToken)
    if (turnThreadRef.current !== contextRef.current.threadId) {
      // The turn belongs to a thread the user has left: regenerate (a busy retry or the
      // notice's Retry) would resend the shown thread's last message instead.
      busyHitRef.current = null
      hitRetryableRef.current = false
      if (status === "error") clearError()
      return
    }
    if (status === "error" && !busyError) {
      busyHitRef.current = null
      return
    }

    // A busy 503 is raised before the agent runs or writes a checkpoint, so resending
    // is safe and gets the full budget. A busy stream part comes after the turn was
    // checkpointed (and maybe after tools ran), and each regenerate appends the user
    // message again, so it gets the single retry the overload path allows.
    const streamBusy = busyHitRef.current
    busyHitRef.current = null
    const busy = streamBusy ?? (busyError ? { retryAfter: BUSY_RETRY_AFTER_SECONDS } : null)
    const maxBusyRetries = streamBusy ? 1 : BUSY_MAX_AUTO_RETRIES
    if (busy) {
      hitRetryableRef.current = false
      if (busyAttemptsRef.current < maxBusyRetries) {
        busyAttemptsRef.current += 1
        busyTracker.startRetry(busyToken)
        busyTimerRef.current = setTimeout(() => {
          busyTimerRef.current = null
          void regenerate()
        }, busyRetryDelayMs(busy.retryAfter, busyAttemptsRef.current))
      } else {
        busyAttemptsRef.current = 0
        setBusyNotice(true)
      }
      return
    }
    busyAttemptsRef.current = 0

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
  }, [status, regenerate, busyToken, busyError, clearError])

  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight
    }
  }, [messages])

  function handleSend(text: string) {
    resetOverloadState()
    setStoppedNotice(false)
    turnThreadRef.current = threadId
    sendMessage({ text })
  }

  function handleStop() {
    setStoppedNotice(true)
    cancelBusyRetry()
    void stop()
  }

  // regenerate resends the last user message, dropping any partial reply to it. After a
  // mid-stream failure the turn was already checkpointed, so the backend records the
  // message twice, as with a manual resend or the overload retry.
  function handleRetry() {
    resetOverloadState()
    setStoppedNotice(false)
    turnThreadRef.current = threadId
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
      <div className="flex h-full min-w-0 flex-col">
        {loadBanners}
        {staleBanner}
        <div className="min-h-0 flex-1">
          <ChatEmptyState
            input={input}
            setInput={setInput}
            onSend={handleSend}
            disabled={isStreaming}
          />
        </div>
      </div>
    )
  }

  return (
    <div className="flex h-full min-w-0">
      <div className="flex min-w-0 flex-1 flex-col">
        <ChatThreadHeader
          title={threadTitle}
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
          {stoppedNotice && <ChatStoppedNotice />}
          {error && !busyError && (
            <ChatErrorNotice
              error={error}
              onStartNewThread={startFreshThread}
              onRetry={handleRetry}
              pathPrefix={pathPrefix}
            />
          )}
          {overloadNotice && <ChatOverloadNotice onRetry={handleRetry} />}
          {busyNotice && <ChatBusyNotice onRetry={handleRetry} />}
        </div>

        {/* Materialization progress banner — always visible when a job is active for this thread */}
        {activeMaterializationJob
          && (activeMaterializationJob.state === "pending" || activeMaterializationJob.state === "running")
          && activeDomainId && (
            <MaterializationProgressBanner
              key={activeMaterializationJob.thread_job_id}
              job={activeMaterializationJob}
              workspaceId={activeDomainId}
            />
          )}

        {loadBanners}
        {staleBanner}
        <SourceFreshness freshness={freshness} />

        {/* Input area */}
        <div className="border-t p-4">
          <ChatComposer
            input={input}
            setInput={setInput}
            onSend={handleSend}
            isStreaming={isStreaming}
            onStop={handleStop}
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
        canvas={threadPanelOpen ? (
          <ChatCanvasPanel workspaceId={activeDomainId} threadId={threadId} />
        ) : null}
      />
    </div>
  )
}
