import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { AttachmentUploadError, uploadChatAttachment } from "./chatAttachments"

class FakeXHR {
  static last: FakeXHR
  method = ""
  url = ""
  headers: Record<string, string> = {}
  body: FormData | null = null
  status = 0
  responseText = ""
  responseType = ""
  withCredentials = false
  upload: { onprogress: ((e: { lengthComputable: boolean; loaded: number; total: number }) => void) | null } =
    { onprogress: null }
  onload: (() => void) | null = null
  onerror: (() => void) | null = null
  onabort: (() => void) | null = null
  ontimeout: (() => void) | null = null
  timeout = 0

  constructor() {
    FakeXHR.last = this
  }
  open(method: string, url: string) {
    this.method = method
    this.url = url
  }
  setRequestHeader(name: string, value: string) {
    this.headers[name] = value
  }
  send(body: FormData) {
    this.body = body
  }
  abort() {
    this.onabort?.()
  }
  respond(status: number, body: unknown) {
    this.status = status
    this.responseText = JSON.stringify(body)
    this.onload?.()
  }
}

const file = new File(["{}"], "registry.json", { type: "application/json" })

beforeEach(() => {
  vi.stubGlobal("XMLHttpRequest", FakeXHR)
  document.cookie = "csrftoken_scout=tok"
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe("uploadChatAttachment", () => {
  it("posts the file and workspace as multipart with CSRF, reporting progress", async () => {
    const progress = vi.fn()
    const result = uploadChatAttachment(file, "ws-1", progress, new AbortController().signal)
    const xhr = FakeXHR.last
    expect(xhr.method).toBe("POST")
    expect(xhr.url).toBe("/api/chat/attachments/")
    expect(xhr.headers["X-CSRFToken"]).toBe("tok")
    expect(xhr.body?.get("workspaceId")).toBe("ws-1")
    expect((xhr.body?.get("file") as File).name).toBe("registry.json")

    xhr.upload.onprogress?.({ lengthComputable: true, loaded: 1, total: 4 })
    expect(progress).toHaveBeenCalledWith(0.25)

    const body = { id: "a", filename: "registry.json", mediaType: "application/json", size: 2, url: "/api/chat/attachments/a/" }
    xhr.respond(201, body)
    await expect(result).resolves.toEqual(body)
  })

  it("rejects with the server's message and reason", async () => {
    const result = uploadChatAttachment(file, "ws-1", vi.fn(), new AbortController().signal)
    FakeXHR.last.respond(400, { error: "Images can be at most 5 MB.", reason: "attachment_too_large" })
    await expect(result).rejects.toEqual(
      expect.objectContaining({ message: "Images can be at most 5 MB.", reason: "attachment_too_large" }),
    )
    await expect(result).rejects.toBeInstanceOf(AttachmentUploadError)
  })

  it("treats a success body missing contract fields as a failed upload", async () => {
    const result = uploadChatAttachment(file, "ws-1", vi.fn(), new AbortController().signal)
    FakeXHR.last.respond(201, { id: "a", url: "/api/chat/attachments/a/", media_type: "application/json" })
    await expect(result).rejects.toBeInstanceOf(AttachmentUploadError)
    await expect(result).rejects.toMatchObject({ message: expect.stringContaining("not understood") })
  })

  it("shows the busy prose, not its machine code, when Scout is at capacity", async () => {
    const result = uploadChatAttachment(file, "ws-1", vi.fn(), new AbortController().signal)
    FakeXHR.last.respond(503, { error: "busy", message: "Scout is busy, try again shortly." })
    await expect(result).rejects.toMatchObject({ message: "Scout is busy, try again shortly." })
  })

  it("gives up on a stalled upload", async () => {
    const result = uploadChatAttachment(file, "ws-1", vi.fn(), new AbortController().signal)
    expect(FakeXHR.last.timeout).toBeGreaterThan(0)
    FakeXHR.last.ontimeout?.()
    await expect(result).rejects.toMatchObject({ message: expect.stringContaining("timed out") })
  })

  it("aborts with its signal", async () => {
    const controller = new AbortController()
    const result = uploadChatAttachment(file, "ws-1", vi.fn(), controller.signal)
    controller.abort()
    await expect(result).rejects.toMatchObject({ name: "AbortError" })
  })
})
