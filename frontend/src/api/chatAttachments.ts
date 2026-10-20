import { withBasePath } from "@/config"
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
      const message =
        typeof body?.error === "string" && body.error.trim()
          ? body.error
          : xhr.status === 413
            ? "Too large to upload."
            : `Upload failed (HTTP ${xhr.status}).`
      reject(new AttachmentUploadError(message, reason))
    }
    const form = new FormData()
    form.append("file", file, file.name)
    form.append("workspaceId", workspaceId)
    xhr.send(form)
  })
}
