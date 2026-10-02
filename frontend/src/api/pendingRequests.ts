import { api, ApiError } from "@/api/client"
import type { PendingRequest } from "@/api/jobs"

/** Joins the parts the way the server sends them as one message. */
export const PART_SEPARATOR = "\n\n"

export type PendingPhase = "waiting" | "answering" | "unanswered"

function base(workspaceId: string, threadId: string): string {
  return `/api/workspaces/${workspaceId}/threads/${threadId}/pending-request`
}

export const pendingRequestApi = {
  addPart: (workspaceId: string, threadId: string, part: { id: string; text: string }) =>
    api.post<PendingRequest>(`${base(workspaceId, threadId)}/parts/`, part),
  discard: (workspaceId: string, threadId: string, version: number) =>
    api.delete<{ status: string }>(`${base(workspaceId, threadId)}/`, { version }),
}

/** A 409: the request was claimed, changed or settled since this client saw it. */
export function isPendingConflict(error: unknown): boolean {
  return error instanceof ApiError && error.status === 409
}

export function pendingRequestText(pending: PendingRequest): string {
  return pending.parts.map((part) => part.text).join(PART_SEPARATOR)
}

/**
 * Where the request stands: still waiting on its load, being answered, or left
 * unanswered because its load ended without sending it (the user sends or drops it).
 */
export function pendingPhase(pending: PendingRequest): PendingPhase {
  // A stopped load still resumes the chat and sends the request with its reply.
  if (
    pending.state === "claimed" ||
    pending.thread_job_state === "running" ||
    pending.thread_job_state === "cancelled"
  ) {
    return "answering"
  }
  if (pending.thread_job_state === "pending") return "waiting"
  return "unanswered"
}
