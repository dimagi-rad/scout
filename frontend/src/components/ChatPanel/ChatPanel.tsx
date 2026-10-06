import { Chat, useChat } from "@ai-sdk/react"
import { DefaultChatTransport, generateId, type UIMessage } from "ai"
import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import { useLocation } from "react-router-dom"
import { getCsrfToken, api, ApiError } from "@/api/client"
import { BASE_PATH } from "@/config"
import { useAppStore } from "@/store/store"
import { forgetLocalThread, isLocalThread } from "@/store/localThreads"
import { turnArtifactOwners } from "@/components/ChatMessage/artifactReferences"
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
import {
  TITLE_REFRESH_DELAYS_MS,
  scheduleTitleRefresh,
  useGeneratedTitleRefresh,
  type TitleRefreshTrigger,
} from "./useGeneratedTitleRefresh"
import { readDraft, writeDraft } from "./draftStorage"
import { classifyChatError } from "./chatErrors"
import { PendingRequestCard } from "./PendingRequestCard"
import { useHeldRequest, type EditOutcome } from "./useHeldRequest"
import { useResumeStream } from "./useResumeStream"
import { HISTORY_LOAD_TIMEOUT_MS } from "./historyLoad"
import { Loader2 } from "lucide-react"
import { Button } from "@/components/ui/button"
import { isChatRunning, threadChatKey, useThreadChat } from "./threadChats"
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

interface HeldSend {
  messageId: string
  version: number
  requestId: string
  workspaceId: string | null
  threadId: string
  /** What the user typed with it, which only this message carries. */
  extra?: string
  /** A reply began streaming, so the server took the message: never undo it. */
  streamed: boolean
}

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
  const prevStatusRef = useRef<{ chatKey: string; status: string }>({ chatKey: "", status: "" })
  // Transient-overload auto-retry bookkeeping; see ./overloadRetry.
  const hitRetryableRef = useRef(false)
  // Chats whose turn already used its one overload retry; per chat, as a left
  // chat's retry can still be running.
  const [retriedChats] = useState(() => new Set<string>())
  const prevRetryStatusRef = useRef<{ chatKey: string; status: string }>({
    chatKey: "",
    status: "",
  })
  const [overloadNotice, setOverloadNotice] = useState(false)
  // Connection-limit "busy" turns; the shared BusyNotice shows their progress.
  const busyHitRef = useRef<{ retryAfter: number | null } | null>(null)
  // Per chat, so leaving and returning mid-turn can't refill a stream-busy retry that
  // would append the user message again.
  const [busyAttempts] = useState(() => new Map<string, number>())
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
    setPendingRequest,
    refresh: refreshJobs,
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

  // The shown chat, for callbacks and async results that may land after a switch.
  const contextRef = useRef({ workspaceId: activeDomainId, threadId })
  contextRef.current = { workspaceId: activeDomainId, threadId }
  const historyLoadingRef = useRef(false)
  const pathPrefix = useLocation().pathname.startsWith("/embed") ? "/embed" : ""

  const held = useHeldRequest(activeDomainId, threadId)
  // A background resume of this chat is answering: its held request is being
  // sent, or its load's ThreadJob is RUNNING (the resume phase).
  const resumeAnswering =
    held.phase === "answering" || activeMaterializationJob?.state === "running"
  const resumeStream = useResumeStream(activeDomainId, threadId, resumeAnswering)
  const resetResumeStreamRef = useRef(resumeStream.reset)
  resetResumeStreamRef.current = resumeStream.reset
  // Per thread, the user message that sends a held request itself ("Send now"), and
  // the request version it showed; a retry of that message names the version too.
  // Each thread's chat runs on its own, so another thread's send must not replace it.
  const [heldSends] = useState(() => new Map<string, HeldSend>())
  // The chat whose server history is loaded; a fresh chat is empty until then.
  const [loaded, setLoaded] = useState<{ chat: Chat<UIMessage> | null; reloadKey: number }>({
    chat: null,
    reloadKey: 0,
  })
  // The load that failed or timed out, offered for retry.
  const [historyFailed, setHistoryFailed] =
    useState<{ chat: Chat<UIMessage>; reloadKey: number } | null>(null)
  // A left chat can finish after the panel is gone; it must not start polls then.
  const mountedRef = useRef(true)
  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
    }
  }, [])
  // Title polls for threads whose turn finished out of view.
  const [titleTimers] = useState(() => new Map<string, () => void>())
  useEffect(() => () => {
    for (const cancel of titleTimers.values()) cancel()
    titleTimers.clear()
  }, [titleTimers])
  const heldHandlerRef = useRef(held.onHeld)
  heldHandlerRef.current = held.onHeld
  const settleSendRef = useRef(held.settleSend)
  settleSendRef.current = held.settleSend
  const restoreHeldRef = useRef(held.restore)
  restoreHeldRef.current = held.restore
  const returnToComposerRef = useRef(returnToComposer)
  returnToComposerRef.current = returnToComposer
  // Leaving the chat drops useChat's view of a held send, so nothing would end its
  // hiding; the next poll shows the server's copy instead. Its typed text is not
  // returned here, unlike for an abandoned busy retry: the send's outcome is
  // unknown, and a draft left after one that went out would be sent again.
  useEffect(() => () => {
    for (const sending of heldSends.values()) settleSendRef.current(sending.threadId)
    heldSends.clear()
  }, [heldSends])

  const fetchThreadsRef = useRef(fetchThreads)
  fetchThreadsRef.current = fetchThreads
  const setPendingRequestRef = useRef(setPendingRequest)
  setPendingRequestRef.current = setPendingRequest
  const refreshJobsRef = useRef(refreshJobs)
  refreshJobsRef.current = refreshJobs

  /** The server refused the send: its turn failed before any reply was pushed after it. */
  function heldSendRefused(sending: HeldSend, target: Chat<UIMessage>): boolean {
    return (
      target.status === "error"
      && !sending.streamed
      && target.messages.at(-1)?.id === sending.messageId
    )
  }

  /** The held send is over: settled if the server took it, else undone and handed back. */
  function endHeldSend(sending: HeldSend, refused: boolean, target: Chat<UIMessage>) {
    heldSends.delete(sending.threadId)
    if (!refused) {
      settleSendRef.current(sending.threadId)
      return
    }
    target.messages = target.messages.filter((message) => message.id !== sending.messageId)
    restoreHeldRef.current(sending.threadId)
    if (sending.extra) {
      returnToComposerRef.current(sending.workspaceId, sending.threadId, sending.extra)
    }
  }

  /** The shown thread polls for its generated title; a thread finished out of view does it here. */
  function refreshThreadsAfterBackgroundTurn(workspaceId: string, finishedThreadId: string) {
    if (!mountedRef.current) return
    const stillThere = () => useAppStore.getState().activeDomainId === workspaceId
    const titlePending = () =>
      useAppStore.getState().threads.find((thread) => thread.id === finishedThreadId)
        ?.title_source === "first_message"
    if (!stillThere()) return
    void fetchThreadsRef.current(workspaceId)
    titleTimers.get(finishedThreadId)?.()
    let ticks = 0
    const cancel = scheduleTitleRefresh(() => {
      ticks += 1
      if (ticks === TITLE_REFRESH_DELAYS_MS.length && titleTimers.get(finishedThreadId) === cancel) {
        titleTimers.delete(finishedThreadId)
      }
      if (stillThere() && titlePending()) void fetchThreadsRef.current(workspaceId)
    })
    titleTimers.set(finishedThreadId, cancel)
  }

  const createChat = (
    chatWorkspaceId: string | null,
    chatThreadId: string,
    release: () => void,
  ) => {
    const context = { workspaceId: chatWorkspaceId, threadId: chatThreadId }
    const shown = () =>
      contextRef.current.workspaceId === chatWorkspaceId
      && contextRef.current.threadId === chatThreadId
    const threadChat: Chat<UIMessage> = new Chat<UIMessage>({
      transport: new DefaultChatTransport({
        api: `${BASE_PATH}/api/chat/`,
        credentials: "include",
        headers: () => ({ "X-CSRFToken": getCsrfToken() }),
        body: () => ({ data: context }),
        prepareSendMessagesRequest: ({ body, id, messages, trigger, messageId }) => {
          const sending = heldSends.get(chatThreadId)
          const data =
            sending && messages.at(-1)?.id === sending.messageId
              ? {
                  ...context,
                  pendingRequestVersion: sending.version,
                  pendingRequestId: sending.requestId,
                }
              : context
          return { body: { ...body, data, id, messages, trigger, messageId } }
        },
      }),
      onData: (part) => {
        if (part.type === "data-pending-request") {
          const pending = part.data as PendingRequest
          // onHeld also hides the shown thread's messages; a left chat reloads on return.
          if (shown()) {
            heldHandlerRef.current(pending)
          } else if (contextRef.current.workspaceId === chatWorkspaceId) {
            setPendingRequestRef.current(pending.thread_id, pending)
            void refreshJobsRef.current()
          }
          return
        }
        // Retries act on the shown chat only; a left chat's turn just finishes.
        if (!shown()) return
        const retryAfter = busyRetryAfter(part)
        if (retryAfter !== undefined) busyHitRef.current = { retryAfter }
        else if (isRetryableErrorPart(part)) hitRetryableRef.current = true
      },
      onFinish: () => {
        // The shown chat handles both from its status effects.
        if (shown()) return
        const sending = heldSends.get(chatThreadId)
        // Status is already final here: the SDK sets it before calling onFinish.
        if (sending) endHeldSend(sending, heldSendRefused(sending, threadChat), threadChat)
        release()
        if (chatWorkspaceId) refreshThreadsAfterBackgroundTurn(chatWorkspaceId, chatThreadId)
      },
    })
    return threadChat
  }
  const chat = useThreadChat(activeDomainId, threadId, createChat)
  const chatKey = threadChatKey(activeDomainId, threadId)

  const {
    messages, sendMessage, status, stop, error, setMessages, regenerate, clearError,
  } = useChat({ chat })
  const busyError = error !== undefined && isBusyChatError(error)
  const visibleMessages = useMemo(() => held.hiddenMessageIds.size
    ? withoutHeldMessages(messages, held.hiddenMessageIds)
    : messages, [messages, held.hiddenMessageIds])

  const artifactOwners = useMemo(() => turnArtifactOwners(visibleMessages), [visibleMessages])

  const cancelBusyRetry = useCallback(() => {
    if (busyTimerRef.current) clearTimeout(busyTimerRef.current)
    busyTimerRef.current = null
    busyHitRef.current = null
    busyTracker.settle(busyToken)
  }, [busyToken])

  function resetOverloadState() {
    hitRetryableRef.current = false
    retriedChats.delete(chatKey)
    setOverloadNotice(false)
    setBusyNotice(false)
    busyAttempts.delete(chatKey)
    cancelBusyRetry()
  }

  // A pending busy retry, or a notice whose Retry would regenerate, belongs to this
  // thread; never replay it into another. threadId is the trigger: this cleanup runs
  // on every thread change, so the dependency must stay even though it isn't read.
  // clearError is the left chat's, so its error notice does not wait for a return.
  useEffect(() => () => {
    // A held send waiting on a busy retry, or out of them, is abandoned with it:
    // stop hiding its request, and return text that only the unsent message carried.
    // One still in flight ends in its chat's onFinish instead.
    const sending = heldSends.get(threadId)
    if (sending && (busyTimerRef.current || busyNoticeRef.current)) {
      heldSends.delete(threadId)
      settleSendRef.current(sending.threadId)
      if (!sending.streamed && sending.extra) {
        returnToComposerRef.current(sending.workspaceId, sending.threadId, sending.extra)
      }
    } else if (sending && !isChatRunning(chat)) {
      // Defensive: a turn that ended in the same commit as the switch would be missed
      // by both onFinish (it was still shown) and the status effect (now another thread).
      endHeldSend(sending, heldSendRefused(sending, chat), chat)
      if (activeDomainId) refreshThreadsAfterBackgroundTurn(activeDomainId, threadId)
    }
    cancelBusyRetry()
    hitRetryableRef.current = false
    setBusyNotice(false)
    setOverloadNotice(false)
    clearError()
    // endHeldSend and refreshThreadsAfterBackgroundTurn are per-render but read only refs.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [threadId, chat, heldSends, cancelBusyRetry, clearError])

  const isStreaming = status === "streaming" || status === "submitted"
  // While a thread's history (re)loads it is not a new chat, and no turn may start: a
  // turn racing the load would be duplicated or lost by it. Only a chat this tab just
  // made up (New chat) has no history to wait for; the thread list can't tell, as it
  // loads late and holds only the latest threads.
  const historyLoading =
    activeDomainId !== null
    && !isLocalThread(threadId)
    && !isStreaming
    && !(loaded.chat === chat && loaded.reloadKey === messageReloadKey)
  historyLoadingRef.current = historyLoading
  const historyLoadFailed =
    historyFailed?.chat === chat && historyFailed.reloadKey === messageReloadKey

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
    const reloadKey = messageReloadKey
    // A turn still streaming holds the conversation in memory, ahead of the server's;
    // its history loaded before it could start. A chat this tab just made up has none.
    if (isChatRunning(chat) || isLocalThread(threadId)) {
      writeSavedThreadId(activeDomainId, threadId)
      void Promise.resolve().then(() => {
        if (cancelled) return
        setLoaded({ chat, reloadKey })
        // Nothing was fetched, so a failed load stays failed and keeps its Retry.
        setHistoryFailed((failed) => (failed?.chat === chat ? { chat, reloadKey } : failed))
      })
      return () => { cancelled = true }
    }

    const abort = new AbortController()
    const timeout = setTimeout(() => abort.abort(), HISTORY_LOAD_TIMEOUT_MS)

    async function loadMessages() {
      try {
        const response = await api.get<
          UIMessage[] | { messages: UIMessage[]; pending_request: PendingRequest | null }
        >(
          `/api/workspaces/${activeDomainId}/threads/${threadId}/messages/?include=pending`,
          abort.signal,
        )
        if (cancelled) return
        // A server from before held requests ignores ``include`` and sends the bare list.
        const history = Array.isArray(response)
          ? { messages: response, pending_request: null }
          : response
        setLoaded({ chat, reloadKey })
        // A shown thread can't send until its history loads, so only a new chat (no
        // history) or a retry timer can have started a turn; the live turn wins.
        if (!isChatRunning(chat)) setMessages(history.messages)
        held.onMessagesLoaded(history.pending_request, { keepHidden: isChatRunning(chat) })
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
        // A failure or timeout must not block sending for good: unblock, keep what is
        // shown, and offer a retry.
        setLoaded({ chat, reloadKey })
        setHistoryFailed({ chat, reloadKey })
        resetResumeStreamRef.current()
      } finally {
        clearTimeout(timeout)
      }
    }

    loadMessages()
    return () => {
      cancelled = true
      abort.abort()
      clearTimeout(timeout)
    }
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
  // Keyed by chat: a switch from a streaming chat to an idle one is not a finish.
  useEffect(() => {
    const prev = prevStatusRef.current
    prevStatusRef.current = { chatKey, status }
    if (
      prev.chatKey === chatKey && prev.status === "streaming" && status === "ready" && activeDomainId
    ) {
      fetchThreads(activeDomainId)
      setTitleRefreshTrigger((previous) => ({ threadId, turn: (previous?.turn ?? 0) + 1 }))
      if (threadPanelOpen && threadPanelMode === "files") {
        void loadThreadArtifacts()
      }
    }
  }, [
    chatKey,
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
    prevRetryStatusRef.current = { chatKey, status }
    if (prev.chatKey !== chatKey) return
    const wasRunning = prev.status === "streaming" || prev.status === "submitted"
    // "error" counts only for a busy 503; a hard failure after an overload part must
    // keep its error notice, not be silently re-posted.
    // submitted -> streaming is mid-run; acting on it would drop a busy part that
    // arrived before the first streaming render.
    if (!wasRunning || (status !== "ready" && status !== "error")) return
    // Any finished run releases this thread's "retrying" slot, hard errors included.
    busyTracker.settle(busyToken)
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
      const attempts = busyAttempts.get(chatKey) ?? 0
      if (attempts < maxBusyRetries) {
        busyAttempts.set(chatKey, attempts + 1)
        busyTracker.startRetry(busyToken)
        busyTimerRef.current = setTimeout(() => {
          busyTimerRef.current = null
          void regenerate()
        }, busyRetryDelayMs(busy.retryAfter, attempts + 1))
      } else {
        busyAttempts.delete(chatKey)
        setBusyNotice(true)
      }
      return
    }
    busyAttempts.delete(chatKey)

    const action = decideOverloadAction({
      hitRetryable: hitRetryableRef.current,
      alreadyRetried: retriedChats.has(chatKey),
    })
    hitRetryableRef.current = false
    if (action === "retry") {
      retriedChats.add(chatKey)
      void regenerate()
    } else if (action === "notify") {
      retriedChats.delete(chatKey)
      setOverloadNotice(true)
    } else {
      // The turn (or its retry) finished cleanly.
      retriedChats.delete(chatKey)
    }
  }, [chatKey, status, regenerate, busyToken, busyError, retriedChats, busyAttempts])

  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight
    }
  }, [messages, resumeStream.text])

  // A held send hides its request until the server stops reporting it; once the
  // send is over, the next poll shows the server's copy again (gone, or still
  // there if it never went out). Only a send refused before any reply started is
  // undone here: a reply that failed mid-stream already saved the message.
  useEffect(() => {
    // A send from a chat the user left ends in that chat's onFinish.
    const sending = heldSends.get(threadId)
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
    const refused = heldSendRefused(sending, chat)
    endHeldSend(sending, refused, chat)
    if (!refused) return
    // A notice with Retry would resend whatever turn is now last, so those are
    // cleared and the card is the way on. Notices without one (a final reason, a
    // reconnect remedy, a stale thread) stay: they say what the card cannot.
    const refusal = error ? classifyChatError(error).kind : "generic"
    if (refusal === "generic" || refusal === "access-retry") clearError()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [threadId, status, busyError])

  function sendText(text: string) {
    // A held send left behind its busy notice is replaced by this turn, which
    // carries the held text itself; only the text typed with it would be lost.
    const stale = heldSends.get(threadId)
    if (stale) {
      heldSends.delete(threadId)
      held.settleSend(stale.threadId)
      if (!stale.streamed && stale.extra) {
        returnToComposer(stale.workspaceId, stale.threadId, stale.extra)
      }
    }
    resetOverloadState()
    setStoppedNotice(false)
    forgetLocalThread(threadId)
    void sendMessage({ text })
  }

  /** Send the held request as this turn, with ``extra`` after it. */
  function sendHeld(pending: PendingRequest, extra?: string) {
    const text = extra
      ? `${pendingRequestText(pending)}${PART_SEPARATOR}${extra}`
      : pendingRequestText(pending)
    const messageId = generateId()
    heldSends.set(threadId, {
      messageId,
      version: pending.version,
      requestId: pending.request_id,
      workspaceId: activeDomainId,
      threadId,
      extra,
      streamed: false,
    })
    resetOverloadState()
    setStoppedNotice(false)
    forgetLocalThread(threadId)
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
    // Defensive: the composer blocks this; keep the typed text rather than drop it.
    if (historyLoading && !held.adding) {
      setInput(text)
      return
    }
    if (held.adding) {
      const sentFrom = threadId
      const outcome = await held.add(text)
      if (outcome === "send" && contextRef.current.threadId === sentFrom && historyLoadingRef.current) {
        // The request is gone, but a turn still can't start before the history lands.
        setInput(text)
        return
      }
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
    if (historyLoading) return
    const pending = held.takeForSend()
    if (pending) sendHeld(pending)
  }

  function handleStop() {
    setStoppedNotice(true)
    busyAttempts.delete(chatKey)
    cancelBusyRetry()
    void stop()
  }

  // regenerate resends the last user message, dropping any partial reply to it. After a
  // mid-stream failure the turn was already checkpointed, so the backend records the
  // message twice, as with a manual resend or the overload retry.
  function handleRetry() {
    resetOverloadState()
    setStoppedNotice(false)
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

  // While loading, or after a failed load, it is not shown as a new, empty chat.
  if (visibleMessages.length === 0 && !held.pending && !historyLoading && !historyLoadFailed) {
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
              visibleArtifactIds={artifactOwners.get(msg.id)}
              isActiveMessage={isStreaming && msgIdx === visibleMessages.length - 1}
              workspaceId={activeDomainId ?? undefined}
              threadId={threadId}
              activeMaterializationJob={activeMaterializationJob}
              recentTerminationsByToolCallId={recentTerminationsByToolCallId}
              onRetryDispatched={notifyJobLikelyStarted}
            />
          ))}
          {historyLoading && (
            <div
              className="flex items-center gap-2 text-sm text-muted-foreground"
              data-testid="chat-history-loading"
            >
              <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
              Loading conversation…
            </div>
          )}
          {historyLoadFailed && (
            <div className="flex items-center gap-2 text-sm text-destructive">
              <span>Couldn't load earlier messages.</span>
              <Button
                variant="outline"
                size="sm"
                disabled={isStreaming}
                onClick={() => setMessageReloadKey((k) => k + 1)}
                data-testid="chat-history-retry"
              >
                Retry
              </Button>
            </div>
          )}
          {held.pending && held.phase && (
            <PendingRequestCard
              pending={held.pending}
              phase={held.phase}
              key={threadId}
              onSendNow={handleSendHeldNow}
              onEdit={handleEditHeld}
              onRemovePart={held.removePart}
              onAbandonEdit={(text) => returnToComposer(activeDomainId, threadId, text)}
              actionsDisabled={isStreaming || historyLoading}
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
            sendBlocked={historyLoading}
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
