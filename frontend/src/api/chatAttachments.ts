import { withBasePath } from "@/config"
import { BUSY_MESSAGE, isBusyBody } from "./busy"
import { asRecord, getCsrfToken } from "./client"

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

function uploadErrorMessage(body: Record<string, unknown> | undefined, status: number): string {
  // A capacity 503's ``error`` is the machine code "busy"; its prose is ``message``.
  if (isBusyBody(body)) return typeof body?.message === "string" ? body.message : BUSY_MESSAGE
  if (typeof body?.error === "string" && body.error.trim()) return body.error
  if (status === 413) return "Too large to upload."
  return `Upload failed (HTTP ${status}).`
}

const UPLOAD_TIMEOUT_MS = 120_000

export class AttachmentUploadError extends Error {
  reason: string | null

  constructor(message: string, reason: string | null = null) {
    super(message)
    this.name = "AttachmentUploadError"
    this.reason = reason
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
    xhr.open("POST", withBasePath("/api/chat/attachments/"))
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
      if (xhr.status === 201 && isUploadedAttachment(body)) {
        resolve(body)
        return
      }
      const reason = typeof body?.reason === "string" ? body.reason : null
      reject(new AttachmentUploadError(uploadErrorMessage(body, xhr.status), reason))
    }
    const form = new FormData()
    form.append("file", file, file.name)
    form.append("workspaceId", workspaceId)
    xhr.send(form)
  })
}
