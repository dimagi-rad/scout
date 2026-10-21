import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react"
import { createPortal } from "react-dom"
import { Upload } from "lucide-react"

import { cn } from "@/lib/utils"
import { carriesFiles } from "./rules"

// Only a backstop for a drag that ended outside the window unheard: drop, dragend
// and pointer moves cover the rest. Generous, as some platforms send dragover
// only while the pointer moves, and a user may hold still while aiming.
const DRAG_IDLE_MS = 4_000

/**
 * While a chat is open the whole window takes dropped files: the app's top bar,
 * the sidebar and the artifact panel as well as the chat itself, since a user
 * drags a screenshot onto whatever is nearest.
 *
 * - The drag is noticed on window. A full-window overlay then takes every drag
 *   event, so no element (an artifact iframe included) can swallow the drop, and
 *   crossing elements underneath can't make it flicker. A drag that enters the
 *   window straight onto an iframe is the iframe's document's to see, not ours,
 *   until it crosses onto this page.
 * - The overlay goes on leaving the window, on drop or dragend, at the next
 *   pointer move (none fire during a drag), or when dragover stops arriving. A
 *   dragleave from a node that unmounted mid-drag can't keep it up.
 * - Only drags carrying files count: a text selection, a link, or an image
 *   dragged from the page itself is left alone.
 * - A file dropped while the overlay is not up is still taken here rather than
 *   opened by the browser.
 *
 * So while this is mounted, no other drop target in the page can receive files.
 */
export function ChatDropZone({
  onFiles,
  className,
  children,
}: {
  /** ``folders``: dropped folders, which arrive as File objects but can't be read. */
  onFiles: (files: File[], folders?: File[]) => void
  className?: string
  children: ReactNode
}) {
  const [dragging, setDragging] = useState(false)
  const draggingRef = useRef(false)
  // A drag that began on this page (an image in a reply, say) is never an upload.
  const internalDrag = useRef(false)
  const idleTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const latest = useRef(onFiles)
  useLayoutEffect(() => {
    latest.current = onFiles
  })

  useEffect(() => {
    const show = (on: boolean) => {
      if (idleTimer.current) clearTimeout(idleTimer.current)
      idleTimer.current = on ? setTimeout(() => show(false), DRAG_IDLE_MS) : null
      if (draggingRef.current === on) return
      draggingRef.current = on
      setDragging(on)
    }
    const takes = (e: DragEvent) =>
      !internalDrag.current && carriesFiles(e.dataTransfer)

    const onDragStart = () => {
      internalDrag.current = true
    }
    const onDragEnd = () => {
      internalDrag.current = false
      show(false)
    }
    // Pointer events don't fire during a drag; one now means any drag is over.
    const onPointerMove = () => {
      internalDrag.current = false
      if (draggingRef.current) show(false)
    }
    const onDragEnter = (e: DragEvent) => {
      if (takes(e)) show(true)
    }
    const onDragOver = (e: DragEvent) => {
      if (!carriesFiles(e.dataTransfer)) return
      // Cancelled either way: dropEffect "none" is what stops the browser opening it.
      e.preventDefault()
      if (!e.dataTransfer) return
      if (takes(e)) {
        e.dataTransfer.dropEffect = "copy"
        show(true)
      } else {
        e.dataTransfer.dropEffect = "none"
      }
    }
    const onDragLeave = (e: DragEvent) => {
      // Leaving the window: nothing inside it is entered next.
      if (draggingRef.current && !e.relatedTarget && leftWindow(e)) show(false)
    }
    const onDrop = (e: DragEvent) => {
      if (!carriesFiles(e.dataTransfer)) return
      e.preventDefault()
      const taken = takes(e)
      internalDrag.current = false
      show(false)
      if (!taken) return
      const { files, folders } = splitFolders(e.dataTransfer)
      if (files.length || folders.length) latest.current(files, folders)
    }

    window.addEventListener("dragstart", onDragStart)
    window.addEventListener("dragend", onDragEnd)
    window.addEventListener("pointermove", onPointerMove, { passive: true })
    window.addEventListener("mousemove", onPointerMove, { passive: true })
    window.addEventListener("dragenter", onDragEnter)
    window.addEventListener("dragover", onDragOver)
    window.addEventListener("dragleave", onDragLeave)
    window.addEventListener("drop", onDrop)
    return () => {
      if (idleTimer.current) clearTimeout(idleTimer.current)
      window.removeEventListener("dragstart", onDragStart)
      window.removeEventListener("dragend", onDragEnd)
      window.removeEventListener("pointermove", onPointerMove)
      window.removeEventListener("mousemove", onPointerMove)
      window.removeEventListener("dragenter", onDragEnter)
      window.removeEventListener("dragover", onDragOver)
      window.removeEventListener("dragleave", onDragLeave)
      window.removeEventListener("drop", onDrop)
    }
  }, [])

  return (
    <div className={cn("relative", className)} data-testid="chat-drop-zone">
      {children}
      {dragging
        && createPortal(
          <div
            className="fixed inset-0 z-[100] flex flex-col items-center justify-center gap-2 bg-background/80 p-4 text-primary backdrop-blur-sm"
            // Set here, not inherited: a modal Radix dialog (the artifact panel) puts
            // pointer-events: none on body, and over its iframe the drop must land here.
            style={{ pointerEvents: "auto" }}
            data-testid="chat-drop-overlay"
          >
            <div className="pointer-events-none flex size-full flex-col items-center justify-center gap-2 rounded-xl border-2 border-dashed border-primary">
              <Upload className="size-8" aria-hidden="true" />
              <p className="text-base font-medium">Drop files to attach</p>
              <p className="text-xs text-muted-foreground">
                Images, or JSON, CSV, TSV, TXT and MD files
              </p>
            </div>
          </div>,
          document.body,
        )}
    </div>
  )
}

/**
 * A dropped folder comes through as a File with no readable bytes; its item's entry
 * says it is a directory. Items and files list the dropped files in the same order.
 */
function splitFolders(dataTransfer: DataTransfer | null): { files: File[]; folders: File[] } {
  const all = Array.from(dataTransfer?.files ?? [])
  const items = Array.from(dataTransfer?.items ?? []).filter((item) => item.kind === "file")
  if (items.length !== all.length) return { files: all, folders: [] }
  const files: File[] = []
  const folders: File[] = []
  all.forEach((file, i) => {
    const entry = items[i].webkitGetAsEntry?.()
    if (entry?.isDirectory) folders.push(file)
    else files.push(file)
  })
  return { files, folders }
}

/** Whether a dragleave's coordinates are outside the viewport (or unknown: Safari). */
function leftWindow(e: DragEvent): boolean {
  const { clientX: x, clientY: y } = e
  if (x === 0 && y === 0) return true
  return x <= 0 || y <= 0 || x >= window.innerWidth || y >= window.innerHeight
}
