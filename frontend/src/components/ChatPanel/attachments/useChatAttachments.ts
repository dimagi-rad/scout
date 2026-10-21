import { useCallback, useEffect, useRef, useState, type ClipboardEvent } from "react"
import type { FileUIPart } from "ai"

import { uploadChatAttachment, type UploadedAttachment } from "@/api/chatAttachments"
import { downscaleImage } from "./downscale"
import {
  MAX_ATTACHMENTS,
  checkFileType,
  type FileCheck,
  filesToAttachFromPaste,
  sizeError,
  type AttachmentKind,
} from "./rules"

export interface ComposerAttachment {
  localId: string
  filename: string
  kind: AttachmentKind | null
  size: number
  status: "processing" | "uploading" | "ready" | "error"
  /** 0..1 while uploading. */
  progress: number
  error?: string
  /** Object URL of the image for its chip. */
  previewUrl?: string
  uploaded?: UploadedAttachment
}

export interface ChatAttachments {
  items: ComposerAttachment[]
  /** A file is still being prepared or uploaded: sending must wait. */
  busy: boolean
  /** A file failed; it must be removed before sending, so it isn't silently dropped. */
  failed: boolean
  /** Why files were turned away, e.g. over the per-message limit. */
  notice: string | null
  /** ``folders`` get a chip that says they can't be attached. */
  add: (files: File[], folders?: File[]) => void
  remove: (localId: string) => void
  /** Uploaded files can go out with a message. */
  ready: boolean
  /** The uploaded files as message parts; they leave the composer. */
  take: () => FileUIPart[]
  /** Puts already-uploaded files back, in the chat they were sent from (a refused send). */
  restore: (workspaceId: string | null, threadId: string, parts: FileUIPart[]) => void
}

/** Why the attached files hold up a send, if they do. */
export function attachmentsBlockReason(attachments: ChatAttachments | undefined): string | null {
  if (attachments?.busy) return "Waiting for files to finish uploading…"
  if (attachments?.failed) return "Remove the files that couldn't be attached to send"
  return null
}

let nextLocalId = 0

/** One test for "can go out with a message", so `ready` and take() never disagree. */
function isSendable(item: ComposerAttachment): boolean {
  return item.status === "ready" && item.uploaded !== undefined
}

const TOO_MANY = `You can attach up to ${MAX_ATTACHMENTS} files to a message.`

function pastedName(mediaType: string): string {
  const ext = { "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif" }[mediaType] ?? "png"
  return `pasted-image.${ext}`
}

const NONE: ComposerAttachment[] = []

/**
 * Files attached in the composer, kept per chat like its draft: an upload that
 * finishes after a switch lands in the chat it was added to.
 */
export function useChatAttachments(workspaceId: string | null, threadId: string): ChatAttachments {
  const scope = `${workspaceId ?? ""}:${threadId}`
  const [byScope, setByScope] = useState<Record<string, ComposerAttachment[]>>({})
  const [notice, setNotice] = useState<{ scope: string; text: string } | null>(null)
  const controllers = useRef(new Map<string, AbortController>())
  // The latest state, written by every change before React renders it: a drop and a
  // paste (or a remove and an add) can land in one tick, and each must see the other.
  const byScopeRef = useRef(byScope)

  useEffect(() => {
    const live = controllers.current
    return () => {
      for (const controller of live.values()) controller.abort()
      live.clear()
      for (const items of Object.values(byScopeRef.current)) {
        for (const item of items) if (item.previewUrl) URL.revokeObjectURL(item.previewUrl)
      }
    }
  }, [])

  const items = byScope[scope] ?? NONE

  const setList = useCallback(
    (inScope: string, change: (list: ComposerAttachment[]) => ComposerAttachment[]) => {
      const current = byScopeRef.current
      const rest = { ...current }
      delete rest[inScope]
      const next = change(current[inScope] ?? NONE)
      byScopeRef.current = next.length ? { ...rest, [inScope]: next } : rest
      setByScope(byScopeRef.current)
    },
    [],
  )

  const update = useCallback(
    (inScope: string, localId: string, patch: Partial<ComposerAttachment>) => {
      const list = byScopeRef.current[inScope]
      if (!list?.some((item) => item.localId === localId)) return
      setList(inScope, (items) =>
        items.map((item) => (item.localId === localId ? { ...item, ...patch } : item)),
      )
    },
    [setList],
  )

  const clearNotice = useCallback(
    (inScope: string) => setNotice((current) => (current?.scope === inScope ? null : current)),
    [],
  )

  const process = useCallback(
    async (inScope: string, item: ComposerAttachment, file: File, kind: AttachmentKind) => {
      const controller = new AbortController()
      controllers.current.set(item.localId, controller)
      try {
        let prepared = file
        if (kind === "image") {
          try {
            prepared = await downscaleImage(file)
          } catch {
            update(inScope, item.localId, { status: "error", error: "Couldn't read this image." })
            return
          }
        }
        if (controller.signal.aborted) return
        const tooBig = sizeError(kind, prepared.size)
        if (tooBig) {
          update(inScope, item.localId, { status: "error", error: tooBig, size: prepared.size })
          return
        }
        // Downscaling can change the type, and with it the name (shot.png -> shot.webp).
        update(inScope, item.localId, {
          status: "uploading",
          size: prepared.size,
          filename: prepared.name,
        })
        const uploaded = await uploadChatAttachment(
          prepared,
          workspaceId ?? "",
          (progress) => {
            // Each render redraws the transcript; only the whole percent is shown.
            const shown = byScopeRef.current[inScope]?.find((i) => i.localId === item.localId)
            if (shown && Math.round(shown.progress * 100) === Math.round(progress * 100)) return
            update(inScope, item.localId, { progress })
          },
          controller.signal,
        )
        update(inScope, item.localId, { status: "ready", progress: 1, uploaded })
      } catch (error) {
        if (controller.signal.aborted) return
        update(inScope, item.localId, {
          status: "error",
          error: error instanceof Error ? error.message : "Upload failed.",
        })
      } finally {
        controllers.current.delete(item.localId)
      }
    },
    [update, workspaceId],
  )

  const add = useCallback(
    (dropped: File[], folders: File[] = []) => {
      const files = [...dropped, ...folders]
      // No workspace means no chat to attach to; the panel shows no composer then.
      if (!files.length || !workspaceId) return
      const existing = byScopeRef.current[scope]?.length ?? 0
      const room = Math.max(0, MAX_ATTACHMENTS - existing)
      const accepted = files.slice(0, room)
      if (accepted.length < files.length) {
        setNotice({ scope, text: TOO_MANY })
      } else {
        clearNotice(scope)
      }
      if (!accepted.length) return
      const added = accepted.map((file) => {
        const check: FileCheck = folders.includes(file)
          ? { ok: false, error: "Folders aren't supported. Attach the files inside it instead." }
          : checkFileType(file)
        const item: ComposerAttachment = {
          localId: `attachment-${++nextLocalId}`,
          filename: file.name || pastedName(file.type),
          kind: check.ok ? check.kind : null,
          size: file.size,
          status: check.ok ? "processing" : "error",
          progress: 0,
          error: check.ok ? undefined : check.error,
          previewUrl: check.ok && check.kind === "image" ? URL.createObjectURL(file) : undefined,
        }
        return check.ok
          ? { item, file, kind: check.kind, mediaType: check.mediaType }
          : { item, file, kind: null, mediaType: file.type }
      })
      const addedItems = added.map(({ item }) => item)
      setList(scope, (list) => [...list, ...addedItems])
      for (const { item, file, kind, mediaType } of added) {
        if (!kind) continue
        // A pasted screenshot is often unnamed, and browsers often type .md, .tsv and
        // .csv as "": upload with the chip's name and the type the extension gave.
        const named =
          file.name && file.type === mediaType
            ? file
            : new File([file], item.filename, { type: mediaType })
        void process(scope, item, named, kind)
      }
    },
    [clearNotice, process, scope, setList, workspaceId],
  )

  const remove = useCallback(
    (localId: string) => {
      controllers.current.get(localId)?.abort()
      controllers.current.delete(localId)
      clearNotice(scope)
      const removed = byScopeRef.current[scope]?.find((item) => item.localId === localId)
      if (removed?.previewUrl) URL.revokeObjectURL(removed.previewUrl)
      setList(scope, (list) => list.filter((item) => item.localId !== localId))
    },
    [clearNotice, scope, setList],
  )

  const take = useCallback((): FileUIPart[] => {
    const list = byScopeRef.current[scope] ?? NONE
    const sending = list.filter(isSendable)
    const parts = sending.map((item): FileUIPart => ({
      type: "file",
      mediaType: item.uploaded!.mediaType,
      filename: item.uploaded!.filename,
      url: item.uploaded!.url,
    }))
    if (!parts.length) return parts
    // Only what is sent leaves; a file still uploading (or failed) stays for the next message.
    const sent = new Set(sending.map((item) => item.localId))
    for (const item of list) {
      if (sent.has(item.localId) && item.previewUrl) URL.revokeObjectURL(item.previewUrl)
    }
    clearNotice(scope)
    setList(scope, (current) => current.filter((item) => !sent.has(item.localId)))
    return parts
  }, [clearNotice, scope, setList])

  const restore = useCallback(
    (inWorkspace: string | null, inThread: string, parts: FileUIPart[]) => {
      const inScope = `${inWorkspace ?? ""}:${inThread}`
      const restored = parts.map((part): ComposerAttachment => ({
        localId: `attachment-${++nextLocalId}`,
        filename: part.filename ?? "attachment",
        kind: typeof part.mediaType === "string" && part.mediaType.startsWith("image/") ? "image" : "text",
        size: 0,
        status: "ready",
        progress: 1,
        uploaded: {
          id: "",
          filename: part.filename ?? "attachment",
          mediaType: part.mediaType,
          size: 0,
          url: part.url,
        },
      }))
      // Files added since the send keep their place; what does not fit is said, not lost.
      const room = MAX_ATTACHMENTS - (byScopeRef.current[inScope]?.length ?? 0)
      if (room < restored.length) {
        setNotice({
          scope: inScope,
          text: `${restored.length - Math.max(room, 0)} of the files from the message that wasn't sent didn't fit back: a message takes up to ${MAX_ATTACHMENTS}.`,
        })
      }
      if (room <= 0) return
      setList(inScope, (list) => [...list, ...restored.slice(0, room)])
    },
    [setList],
  )

  return {
    items,
    busy: items.some((item) => item.status === "processing" || item.status === "uploading"),
    failed: items.some((item) => item.status === "error"),
    ready: items.some(isSendable),
    notice: notice?.scope === scope ? notice.text : null,
    add,
    remove,
    take,
    restore,
  }
}

/** A paste of files (a screenshot) attaches them; any other paste is left alone. */
export function attachPastedFiles(e: ClipboardEvent, attachments: ChatAttachments | undefined) {
  if (!attachments) return
  const files = filesToAttachFromPaste(e.clipboardData)
  if (!files.length) return
  e.preventDefault()
  attachments.add(files)
}
