import { useChat } from "@ai-sdk/react"
import { DefaultChatTransport, generateId, type UIMessage } from "ai"
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
import type { PendingRequest } from "@/api/jobs"
import { PART_SEPARATOR, pendingRequestText } from "@/api/pendingRequests"
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
import { useGeneratedTitleRefresh, type TitleRefreshTrigger } from "./useGeneratedTitleRefresh"
import { readDraft, writeDraft } from "./draftStorage"
import { classifyChatError } from "./chatErrors"
import { PendingRequestCard } from "./PendingRequestCard"
import { useHeldRequest, type EditOutcome } from "./useHeldRequest"
import { useResumeStream } from "./useResumeStream"
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

/** Drops the messages a held request took in, and the empty reply each held turn left. */
function withoutHeldMessages(messages: UIMessage[], heldIds: ReadonlySet<string>): UIMessage[] {
  return messages.filter((message, index) => {
    if (heldIds.has(message.id)) return false
    const previous = messages[index - 1]
    return !(
      message.role === "assistant" &&
      previous !== undefined &&
      heldIds.has(previous.id) &&
      message.parts.every((part) => part.type === "step-start" || part.type.startsWith("data-"))
    )
  })
}

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
  // Read after an await, when the render-time value may be stale.
  const inputRef = useRef(input)
  inputRef.current = input
  const [addFailed, setAddFailed] = useState<string | null>(null)
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
  // Read by the thread-change cleanup, which sees only the first render's state.
  const busyNoticeRef = useRef(busyNotice)
  busyNoticeRef.current = busyNotice
  const [stoppedNotice, setStoppedNotice] = useState(false)
  const [titleRefreshTrigger, setTitleRefreshTrigger] = useState<TitleRefreshTrigger | null>(null)

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
  useGeneratedTitleRefresh({
    workspaceId: activeDomainId,
    threadId,
    titlePending: currentThread?.title_source === "first_message",
    trigger: titleRefreshTrigger,
    refresh: fetchThreads,
  })

  // Use a ref so the transport body closure always reads fresh values,
  // even though useChat caches the transport from the first render.
  const contextRef = useRef({ workspaceId: activeDomainId, threadId })
  contextRef.current = { workspaceId: activeDomainId, threadId }
  // The thread whose turn useChat is running. A switch does not abort it, so its
  // outcome can land while another thread is shown.
  const turnThreadRef = useRef<string | null>(null)
  const pathPrefix = useLocation().pathname.startsWith("/embed") ? "/embed" : ""

  const held = useHeldRequest(activeDomainId, threadId)
  // A background resume of this chat is answering: its held request is being
  // sent, or its load's ThreadJob is RUNNING (the resume phase).
  const resumeAnswering =
    held.phase === "answering" || activeMaterializationJob?.state === "running"
  const resumeStream = useResumeStream(activeDomainId, threadId, resumeAnswering)
  const resetResumeStreamRef = useRef(resumeStream.reset)
  resetResumeStreamRef.current = resumeStream.reset
  // The user message that sends a held request itself ("Send now"), and the
  // request version it showed; a retry of that message names the version too.
  const heldSendRef = useRef<{
    messageId: string
    version: number
    requestId: string
    workspaceId: string | null
    threadId: string
    /** What the user typed with it, which only this message carries. */
    extra?: string
    /** A reply began streaming, so the server took the message: never undo it. */
    streamed: boolean
  } | null>(null)
  const heldHandlerRef = useRef(held.onHeld)
  heldHandlerRef.current = held.onHeld
  const settleSendRef = useRef(held.settleSend)
  settleSendRef.current = held.settleSend
  const returnToComposerRef = useRef(returnToComposer)
  returnToComposerRef.current = returnToComposer
  // Leaving the chat drops useChat's view of a held send, so nothing would end its
  // hiding; the next poll shows the server's copy instead. Its typed text is not
  // returned here, unlike for an abandoned busy retry: the send's outcome is
  // unknown, and a draft left after one that went out would be sent again.
  useEffect(() => () => {
    const sending = heldSendRef.current
    if (sending) settleSendRef.current(sending.threadId)
  }, [])

  const [transport] = useState(
    () =>
      new DefaultChatTransport({
        api: `${BASE_PATH}/api/chat/`,
        credentials: "include",
        headers: () => ({ "X-CSRFToken": getCsrfToken() }),
        body: () => ({ data: contextRef.current }),
        prepareSendMessagesRequest: ({ body, id, messages, trigger, messageId }) => {
          const sending = heldSendRef.current
          const data =
            sending && messages.at(-1)?.id === sending.messageId
              ? {
                  ...contextRef.current,
                  pendingRequestVersion: sending.version,
                  pendingRequestId: sending.requestId,
                }
              : contextRef.current
          return { body: { ...body, data, id, messages, trigger, messageId } }
        },
      }),
  )

  const {
    messages, sendMessage, status, stop, error, setMessages, regenerate, clearError,
  } = useChat({
    transport,
    onData: (part) => {
      if (part.type === "data-pending-request") {
        heldHandlerRef.current(part.data as PendingRequest)
        return
      }
      const retryAfter = busyRetryAfter(part)
      if (retryAfter !== undefined) busyHitRef.current = { retryAfter }
      else if (isRetryableErrorPart(part)) hitRetryableRef.current = true
    },
  })
  const busyError = error !== undefined && isBusyChatError(error)
  const visibleMessages = held.hiddenMessageIds.size
    ? withoutHeldMessages(messages, held.hiddenMessageIds)
    : messages

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
    // A held send waiting on a busy retry, or out of them, is abandoned with it:
    // stop hiding its request, and return text that only the unsent message carried.
    const sending = heldSendRef.current
    if (sending && (busyTimerRef.current || busyNoticeRef.current)) {
      heldSendRef.current = null
      settleSendRef.current(sending.threadId)
      if (!sending.streamed && sending.extra) {
        returnToComposerRef.current(sending.workspaceId, sending.threadId, sending.extra)
      }
    }
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
        const response = await api.get<
          UIMessage[] | { messages: UIMessage[]; pending_request: PendingRequest | null }
        >(`/api/workspaces/${activeDomainId}/threads/${threadId}/messages/?include=pending`)
        if (cancelled) return
        // A server from before held requests ignores ``include`` and sends the bare list.
        const loaded = Array.isArray(response)
          ? { messages: response, pending_request: null }
          : response
        setMessages(loaded.messages)
        held.onMessagesLoaded(loaded.pending_request)
        // The reloaded conversation carries whatever the resume streamed.
        resetResumeStreamRef.current()
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
    setAddFailed(null)
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
      // The resumed answer can be the thread's first, which is when it gets a title.
      setTitleRefreshTrigger((prev) => ({ threadId, turn: (prev?.turn ?? 0) + 1 }))
    }
  }, [threadId, recentlyCompletedThreadIds, isStreaming])

  // Refresh thread list when streaming finishes so new threads appear.
  useEffect(() => {
    if (prevStatusRef.current === "streaming" && status === "ready" && activeDomainId) {
      fetchThreads(activeDomainId)
      setTitleRefreshTrigger((prev) => ({ threadId, turn: (prev?.turn ?? 0) + 1 }))
      if (threadPanelOpen && threadPanelMode === "files") {
        void loadThreadArtifacts()
      }
    }
    prevStatusRef.current = status
  }, [
    status,
    activeDomainId,
    threadId,
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

  // A held send hides its request until the server stops reporting it; once the
  // send is over, the next poll shows the server's copy again (gone, or still
  // there if it never went out). Only a send refused before any reply started is
  // undone here: a reply that failed mid-stream already saved the message.
  useEffect(() => {
    const sending = heldSendRef.current
    if (!sending) return
    if (status === "streaming") {
      sending.streamed = true
      return
    }
    // Busy: it is retried, or offered as the notice's Retry (naming its version),
    // so it stays hidden here rather than also offered as Send now; leaving the
    // chat ends it (the thread-change cleanup).
    if (status === "error" && busyError) return
    if (status !== "ready" && status !== "error") return
    heldSendRef.current = null
    const refused = status === "error" && !sending.streamed
    if (!refused) {
      held.settleSend(sending.threadId)
      return
    }
    setMessages((current) => current.filter((message) => message.id !== sending.messageId))
    // A notice with Retry would resend whatever turn is now last, so those are
    // cleared and the card is the way on. Notices without one (a final reason, a
    // reconnect remedy, a stale thread) stay, while their chat is the one open:
    // they say what the card cannot.
    const refusal = error ? classifyChatError(error).kind : "generic"
    const elsewhere = sending.threadId !== contextRef.current.threadId
    if (elsewhere || refusal === "generic" || refusal === "access-retry") clearError()
    held.restore(sending.threadId)
    if (sending.extra) returnToComposer(sending.workspaceId, sending.threadId, sending.extra)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status, busyError])

  function sendText(text: string) {
    // A held send left behind its busy notice is replaced by this turn, which
    // carries the held text itself; only the text typed with it would be lost.
    const stale = heldSendRef.current
    if (stale) {
      heldSendRef.current = null
      held.settleSend(stale.threadId)
      if (!stale.streamed && stale.extra) {
        returnToComposer(stale.workspaceId, stale.threadId, stale.extra)
      }
    }
    resetOverloadState()
    setStoppedNotice(false)
    turnThreadRef.current = threadId
    void sendMessage({ text })
  }

  /** Send the held request as this turn, with ``extra`` after it. */
  function sendHeld(pending: PendingRequest, extra?: string) {
    const text = extra
      ? `${pendingRequestText(pending)}${PART_SEPARATOR}${extra}`
      : pendingRequestText(pending)
    const messageId = generateId()
    heldSendRef.current = {
      messageId,
      version: pending.version,
      requestId: pending.request_id,
      workspaceId: activeDomainId,
      threadId,
      extra,
      streamed: false,
    }
    resetOverloadState()
    setStoppedNotice(false)
    turnThreadRef.current = threadId
    void sendMessage({ id: messageId, role: "user", parts: [{ type: "text", text }] })
  }

  /** Puts text the composer had already cleared back, in the chat it was typed in. */
  function returnToComposer(workspaceId: string | null, sentFrom: string, text: string) {
    if (contextRef.current.workspaceId === workspaceId && contextRef.current.threadId === sentFrom) {
      setInput(inputRef.current.trim() ? `${inputRef.current}\n${text}` : text)
      return
    }
    if (!workspaceId || !userId) return
    const scope = { userId, workspaceId, threadId: sentFrom }
    const draft = readDraft(scope)
    writeDraft(scope, draft.trim() ? `${draft}\n${text}` : text)
  }

  async function handleSend(text: string) {
    if (held.adding) {
      const sentFrom = threadId
      const outcome = await held.add(text)
      if (outcome === "added") {
        setAddFailed(null)
        return
      }
      if (contextRef.current.threadId !== sentFrom) {
        // The user moved on meanwhile; keep the text in that chat's draft.
        returnToComposer(activeDomainId, sentFrom, text)
        return
      }
      if (outcome !== "send") {
        returnToComposer(activeDomainId, sentFrom, text)
        setAddFailed(outcome.failed)
        return
      }
    }
    setAddFailed(null)
    const unanswered = held.phase === "unanswered" ? held.takeForSend() : null
    if (unanswered) sendHeld(unanswered, text)
    else sendText(text)
  }

  async function handleEditHeld(text: string, baseVersion: number): Promise<EditOutcome> {
    const editedIn = threadId
    const outcome = await held.edit(text, baseVersion)
    // Too late to change what is being sent: keep the edit as the next message.
    if (outcome === "gone") returnToComposer(activeDomainId, editedIn, text)
    return outcome
  }

  function handleSendHeldNow() {
    const pending = held.takeForSend()
    if (pending) sendHeld(pending)
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

  if (visibleMessages.length === 0 && !held.pending) {
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
          {visibleMessages.map((msg: UIMessage, msgIdx: number) => (
            <ChatMessage
              key={msg.id}
              message={msg}
              isActiveMessage={isStreaming && msgIdx === visibleMessages.length - 1}
              workspaceId={activeDomainId ?? undefined}
              threadId={threadId}
              activeMaterializationJob={activeMaterializationJob}
              recentTerminationsByToolCallId={recentTerminationsByToolCallId}
              onRetryDispatched={notifyJobLikelyStarted}
            />
          ))}
          {held.pending && held.phase && (
            <PendingRequestCard
              pending={held.pending}
              phase={held.phase}
              key={threadId}
              onSendNow={handleSendHeldNow}
              onEdit={handleEditHeld}
              onRemovePart={held.removePart}
              onAbandonEdit={(text) => returnToComposer(activeDomainId, threadId, text)}
              actionsDisabled={isStreaming}
              onDiscard={() => void held.discard()}
            />
          )}
          {resumeStream.text && (
            <div data-testid="resume-stream">
              <ChatMessage
                message={{
                  id: "resume-stream",
                  role: "assistant",
                  parts: [{ type: "text", text: resumeStream.text }],
                }}
                isActiveMessage
                workspaceId={activeDomainId ?? undefined}
                threadId={threadId}
              />
            </div>
          )}
          {addFailed && (
            <p className="text-sm text-destructive" data-testid="pending-request-add-failed">
              {addFailed}
            </p>
          )}
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
            mode={held.adding ? "add" : "send"}
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
