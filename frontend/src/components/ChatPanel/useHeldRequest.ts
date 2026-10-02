import { useCallback, useRef, useState } from "react"

import type { PendingRequest } from "@/api/jobs"
import { ApiError } from "@/api/client"
import {
  isPendingConflict,
  pendingPhase,
  pendingRequestApi,
  type PendingPhase,
} from "@/api/pendingRequests"
import { useWorkspaceJobs } from "@/contexts/WorkspaceJobsContext"

/** "send": the request was claimed or is gone, so the text is a new turn instead. */
export type AddOutcome = "added" | "send" | { failed: string }

const ADD_FAILED_MESSAGE = "Couldn't add that to your request. It's back in the message box."

export interface HeldRequest {
  /** The request to show: the held one, or the one just sent until its answer loads. */
  pending: PendingRequest | null
  phase: PendingPhase | null
  /** Whether a new message joins the request instead of being sent. */
  adding: boolean
  /** Chat messages the server took into the request (a part's id is the id of the
   *  message it came from); the card shows them instead, until messages reload. */
  hiddenMessageIds: ReadonlySet<string>
  onHeld: (pending: PendingRequest) => void
  onMessagesLoaded: (pending: PendingRequest | null) => void
  add: (text: string) => Promise<AddOutcome>
  /** Hide the request before the client sends it itself; ``restore`` undoes it. */
  takeForSend: () => PendingRequest | null
  restore: (sendThreadId: string) => void
  /** The held send in ``sendThreadId`` is over: show the server's copy from the next poll. */
  settleSend: (sendThreadId: string) => void
  discard: () => Promise<void>
}

function newPartId(): string {
  return typeof crypto !== "undefined" && "randomUUID" in crypto
    ? crypto.randomUUID()
    : `part-${Date.now()}-${Math.random().toString(36).slice(2)}`
}

export function useHeldRequest(workspaceId: string | null, threadId: string): HeldRequest {
  const {
    pendingByThreadId,
    setPendingRequest,
    hidePendingRequest,
    forgetPendingRequest,
    refresh,
  } = useWorkspaceJobs()
  const current = pendingByThreadId[threadId] ?? null
  // What the last render saw, adjusted during render so a change never paints a
  // frame without its card. A request the poll stopped reporting was sent: it
  // shows as answering (``sent``) until the reloaded conversation carries it.
  const [seen, setSeen] = useState<{
    threadId: string
    pending: PendingRequest | null
    sent: PendingRequest | null
    hiddenMessageIds: ReadonlySet<string>
    // Set when this tab removed the request itself, which is not a send to wait on.
    removedLocally: boolean
  }>(() => ({
    threadId,
    pending: current,
    sent: null,
    hiddenMessageIds: new Set(),
    removedLocally: false,
  }))
  if (seen.threadId !== threadId) {
    setSeen({
      threadId,
      pending: current,
      sent: null,
      hiddenMessageIds: new Set(),
      removedLocally: false,
    })
  } else if (seen.pending !== current) {
    const sentNow =
      seen.pending && !current && !seen.removedLocally
        ? { ...seen.pending, state: "claimed" as const }
        : null
    setSeen({
      ...seen,
      pending: current,
      sent: current ? null : (sentNow ?? seen.sent),
      removedLocally: false,
    })
  }
  const { sent, hiddenMessageIds } = seen
  const failedPartRef = useRef<{ id: string; text: string } | null>(null)
  const pending = current ?? sent
  const phase = pending ? (current ? pendingPhase(pending) : "answering") : null

  const onHeld = useCallback(
    (held: PendingRequest) => {
      setPendingRequest(held.thread_id, held)
      setSeen((prev) => ({
        ...prev,
        hiddenMessageIds: new Set([
          ...prev.hiddenMessageIds,
          ...held.parts.map((part) => part.id),
        ]),
      }))
      void refresh()
    },
    [setPendingRequest, refresh],
  )

  const onMessagesLoaded = useCallback(
    (loaded: PendingRequest | null) => {
      // The poll and local changes own the request itself: a load that left before
      // a hold or an add must not undo it. A load that found the request gone ends
      // the "answering" overlay, since its messages now carry the request.
      setSeen((prev) => {
        const sent = loaded === null ? null : prev.sent
        return sent === prev.sent && prev.hiddenMessageIds.size === 0
          ? prev
          : { ...prev, sent, hiddenMessageIds: new Set() }
      })
    },
    [],
  )

  const add = useCallback(
    async (text: string): Promise<AddOutcome> => {
      if (!workspaceId || !current) return "send"
      // A resend of text whose add failed reuses its id, so an add that landed
      // although its response was lost is not added twice.
      const retry = failedPartRef.current
      const part = { id: retry?.text === text ? retry.id : newPartId(), text }
      failedPartRef.current = null
      setPendingRequest(
        threadId,
        {
          ...current,
          version: current.version + 1,
          parts: [...current.parts, { ...part, added_at: new Date().toISOString() }],
        },
        "inFlight",
      )
      try {
        const saved = await pendingRequestApi.addPart(workspaceId, threadId, part)
        setPendingRequest(threadId, saved)
        return "added"
      } catch (error) {
        forgetPendingRequest(threadId)
        void refresh()
        // Claimed or gone: it is being (or was) answered, so this is a new turn.
        if (isPendingConflict(error)) return "send"
        failedPartRef.current = part
        // A 400 (the request would be too long) says what to do; it is not transient.
        return {
          failed:
            error instanceof ApiError && error.status === 400
              ? `${error.message}. It's back in the message box.`
              : ADD_FAILED_MESSAGE,
        }
      }
    },
    [workspaceId, threadId, current, setPendingRequest, forgetPendingRequest, refresh],
  )

  const takeForSend = useCallback(() => {
    if (!current) return null
    setSeen((prev) => ({ ...prev, removedLocally: true }))
    hidePendingRequest(threadId, current.request_id)
    return current
  }, [current, threadId, hidePendingRequest])

  const restore = useCallback(
    (sendThreadId: string) => {
      forgetPendingRequest(sendThreadId)
      void refresh()
    },
    [forgetPendingRequest, refresh],
  )

  const settleSend = useCallback(
    (sendThreadId: string) => setPendingRequest(sendThreadId, null),
    [setPendingRequest],
  )

  const discard = useCallback(async () => {
    if (!workspaceId || !current) return
    setSeen((prev) => ({ ...prev, removedLocally: true }))
    setPendingRequest(threadId, null, "inFlight")
    try {
      await pendingRequestApi.discard(workspaceId, threadId, current.version)
      setPendingRequest(threadId, null)
    } catch {
      forgetPendingRequest(threadId)
      void refresh()
    }
  }, [workspaceId, threadId, current, setPendingRequest, forgetPendingRequest, refresh])

  return {
    pending,
    phase,
    adding: phase === "waiting",
    hiddenMessageIds,
    onHeld,
    onMessagesLoaded,
    add,
    takeForSend,
    restore,
    settleSend,
    discard,
  }
}
