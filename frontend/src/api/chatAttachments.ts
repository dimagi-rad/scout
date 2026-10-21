import { withBasePath } from "@/config"
import { BUSY_MESSAGE, isBusyBody } from "./busy"
import { asRecord, getCsrfToken } from "./client"

/** The upload route; an attachment's own url is this plus ``<id>/``. */
export const ATTACHMENTS_PATH = "/api/chat/attachments/"

export function attachmentUrl(id: string): string {
  return `${ATTACHMENTS_PATH}${id}/`
}

/** The id ``attachmentUrl`` put in ``url``, or the url itself when it isn't one. */
export function attachmentIdFromUrl(url: string): string {
  if (!url.startsWith(ATTACHMENTS_PATH) || !url.endsWith("/")) return url
  return url.slice(ATTACHMENTS_PATH.length, -1)
}

const ATTACHMENT_URL = new RegExp(`^${ATTACHMENTS_PATH.replaceAll("/", "\\/")}[0-9a-f-]{36}\\/$`, "i")

/** Our attachment urls only: anything else in a file part is not fetched or linked. */
export function isAttachmentUrl(url: string): boolean {
  return ATTACHMENT_URL.test(url)
}

export interface UploadedAttachment {
  id: string
  filename: string
  mediaType: string
  size: number
  /** Without BASE_PATH, as the chat POST expects it back. */
  url: string
}

function isUploadedAttachment(body: Record<string, unknown> | undefined): body is Record<string, unknown> & UploadedAttachment {
  return (
    body !== undefined
    && typeof body.id === "string"
    && typeof body.filename === "string"
    && typeof body.mediaType === "string"
    && typeof body.size === "number"
    && typeof body.url === "string"
  )
}

function uploadErrorMessage(
  body: Record<string, unknown> | undefined,
  status: number,
  retryAfter: string | null,
): string {
  // A capacity 503's ``error`` is the machine code "busy"; its prose is ``message``.
  if (isBusyBody(body)) return typeof body?.message === "string" ? body.message : BUSY_MESSAGE
  if (body?.reason === "attachment_rate_limited") {
    const seconds = Number(retryAfter)
    return Number.isFinite(seconds) && seconds > 0
      ? `Too many uploads at once. Remove this and attach it again in ${Math.ceil(seconds)}s.`
      : "Too many uploads at once. Remove this and attach it again in a minute."
  }
  if (body?.reason === "attachment_limit") {
    return "You have too many files waiting to be sent. Send or remove some, then attach this again."
  }
  if (typeof body?.error === "string" && body.error.trim()) return body.error
  if (status === 413) return "Too large to upload."
  return `Upload failed (HTTP ${status}).`
}

const UPLOAD_TIMEOUT_MS = 120_000

export class AttachmentUploadError extends Error {
  constructor(message: string) {
    super(message)
    this.name = "AttachmentUploadError"
  }
}

/**
 * POST /api/chat/attachments/. XHR rather than fetch: fetch reports no upload progress.
 * Rejects with an AbortError DOMException when ``signal`` aborts.
 */
export function uploadChatAttachment(
  file: File,
  workspaceId: string,
  onProgress: (fraction: number) => void,
  signal: AbortSignal,
): Promise<UploadedAttachment> {
  return new Promise((resolve, reject) => {
    if (signal.aborted) {
      reject(new DOMException("Upload cancelled", "AbortError"))
      return
    }
    const xhr = new XMLHttpRequest()
    xhr.open("POST", withBasePath(ATTACHMENTS_PATH))
    xhr.withCredentials = true
    xhr.setRequestHeader("X-CSRFToken", getCsrfToken())
    xhr.responseType = "text"
    // A stalled connection would otherwise leave the chip uploading for good.
    xhr.timeout = UPLOAD_TIMEOUT_MS
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable && event.total > 0) onProgress(event.loaded / event.total)
    }
    const onAbort = () => xhr.abort()
    signal.addEventListener("abort", onAbort, { once: true })
    const done = () => signal.removeEventListener("abort", onAbort)
    xhr.onabort = () => {
      done()
      reject(new DOMException("Upload cancelled", "AbortError"))
    }
    xhr.ontimeout = () => {
      done()
      reject(new AttachmentUploadError("Upload timed out. Check your connection and try again."))
    }
    xhr.onerror = () => {
      done()
      reject(new AttachmentUploadError("Upload failed. Check your connection and try again."))
    }
    xhr.onload = () => {
      done()
      let body: Record<string, unknown> | undefined
      try {
        body = asRecord(JSON.parse(xhr.responseText))
      } catch {
        body = undefined
      }
      const ok = xhr.status >= 200 && xhr.status < 300
      if (ok && isUploadedAttachment(body)) {
        resolve(body)
        return
      }
      if (ok) {
        reject(new AttachmentUploadError("Upload failed: the server's reply was not understood."))
        return
      }
      reject(
        new AttachmentUploadError(
          uploadErrorMessage(body, xhr.status, xhr.getResponseHeader("Retry-After")),
        ),
      )
    }
    const form = new FormData()
    form.append("file", file, file.name)
    form.append("workspaceId", workspaceId)
    xhr.send(form)
  })
}
