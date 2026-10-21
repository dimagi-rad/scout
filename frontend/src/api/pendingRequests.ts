import type { FileUIPart } from "ai"

import { attachmentUrl } from "@/api/chatAttachments"
import { api, ApiError } from "@/api/client"
import type { PendingAttachment, PendingRequest } from "@/api/jobs"

/** Joins the parts the way the server sends them as one message. */
export const PART_SEPARATOR = "\n\n"

export type PendingPhase = "waiting" | "answering" | "unanswered"

function base(workspaceId: string, threadId: string): string {
  return `/api/workspaces/${workspaceId}/threads/${threadId}/pending-request`
}

export const pendingRequestApi = {
  addPart: (
    workspaceId: string,
    threadId: string,
    part: { id: string; text: string; files?: FileUIPart[] },
  ) =>
    api.post<PendingRequest>(`${base(workspaceId, threadId)}/parts/`, part),
  discard: (workspaceId: string, threadId: string, version: number) =>
    api.delete<{ status: string }>(`${base(workspaceId, threadId)}/`, { version }),
  /** ``text`` rewrites the whole request; ``remove_part_id`` drops a later part. */
  edit: (
    workspaceId: string,
    threadId: string,
    change: { version: number } & ({ text: string } | { remove_part_id: string }),
  ) => api.patch<PendingRequest>(`${base(workspaceId, threadId)}/`, change),
}

/** A 409: the request was claimed, changed or settled since this client saw it. */
export function isPendingConflict(error: unknown): boolean {
  return error instanceof ApiError && error.status === 409
}

/** The server's reason code on a refused change, e.g. "version" or "pending_request_too_long". */
export function pendingErrorReason(error: unknown): string | null {
  if (!(error instanceof ApiError)) return null
  const reason = (error.body as { reason?: unknown } | null | undefined)?.reason
  return typeof reason === "string" ? reason : null
}

/** Joins the text as the server does: an attachments-only part adds no text and no separator. */
export function joinPartTexts(texts: string[]): string {
  return texts.filter((text) => text).join(PART_SEPARATOR)
}

export function pendingRequestText(pending: PendingRequest): string {
  return joinPartTexts(pending.parts.map((part) => part.text))
}

export function pendingAttachmentPart(attachment: PendingAttachment): FileUIPart {
  return {
    type: "file",
    mediaType: attachment.media_type,
    filename: attachment.filename,
    url: attachmentUrl(attachment.id),
  }
}

/** The request's attachments across its parts, each once. */
export function pendingRequestAttachments(pending: PendingRequest): PendingAttachment[] {
  const byId = new Map<string, PendingAttachment>()
  for (const part of pending.parts) {
    for (const attachment of part.attachments ?? []) {
      if (!byId.has(attachment.id)) byId.set(attachment.id, attachment)
    }
  }
  return [...byId.values()]
}

/** The request's attachments as message parts, each once. */
export function pendingRequestFiles(pending: PendingRequest): FileUIPart[] {
  return pendingRequestAttachments(pending).map(pendingAttachmentPart)
}

/**
 * Where the request stands: still waiting on its load, being answered, or left
 * unanswered because its load ended without sending it (the user sends or drops it).
 */
export function pendingPhase(pending: PendingRequest): PendingPhase {
  // A stopped load usually resumes and sends the request, but one stopped while
  // still queued never does; offering Send now beats a card stuck on "Answering…",
  // and the resume (which holds the thread) refuses a Send now it already served.
  if (pending.state === "claimed" || pending.thread_job_state === "running") return "answering"
  if (pending.thread_job_state === "pending") return "waiting"
  if (pending.thread_job_id === null && pending.workspace_load_pending) return "waiting"
  return "unanswered"
}
