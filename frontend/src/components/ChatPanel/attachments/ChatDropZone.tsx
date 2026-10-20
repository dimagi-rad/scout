import { useEffect, useRef, useState, type DragEvent, type ReactNode } from "react"
import { Upload } from "lucide-react"

import { cn } from "@/lib/utils"
import { carriesFiles } from "./rules"

/**
 * Makes the whole chat panel (thread header and empty state included) a drop
 * target for files, with an overlay while files are dragged over it.
 *
 * - dragenter/dragleave fire for every child crossed, so a depth counter, not
 *   the last event, decides whether the pointer is still inside.
 * - Only drags carrying files count: a text selection or a link dragged around
 *   the page must not show the overlay or be swallowed.
 * - While mounted, a file dropped anywhere else on the page is ignored instead of
 *   the browser navigating away to open it.
 */
export function ChatDropZone({
  onFiles,
  disabled = false,
  className,
  children,
}: {
  onFiles: (files: File[]) => void
  disabled?: boolean
  className?: string
  children: ReactNode
}) {
  const [dragging, setDragging] = useState(false)
  const depth = useRef(0)
  // A drag that began on this page (an image in a reply, say) is never an upload.
  const internalDrag = useRef(false)

  useEffect(() => {
    const reset = () => {
      depth.current = 0
      internalDrag.current = false
      setDragging(false)
    }
    const onDragStart = () => {
      internalDrag.current = true
    }
    // Bubbles up after the zone's own handlers: a drag the zone took is already
    // defaultPrevented. Anywhere else, cancelling dragover with dropEffect "none"
    // is what stops the browser from opening the file.
    const swallowFileDrop = (e: globalThis.DragEvent) => {
      if (!carriesFiles(e.dataTransfer)) return
      if (e.type === "dragover" && e.dataTransfer && !e.defaultPrevented) {
        e.dataTransfer.dropEffect = "none"
      }
      e.preventDefault()
      if (e.type === "drop") reset()
    }
    window.addEventListener("dragstart", onDragStart)
    window.addEventListener("dragend", reset)
    window.addEventListener("dragover", swallowFileDrop)
    window.addEventListener("drop", swallowFileDrop)
    return () => {
      window.removeEventListener("dragstart", onDragStart)
      window.removeEventListener("dragend", reset)
      window.removeEventListener("dragover", swallowFileDrop)
      window.removeEventListener("drop", swallowFileDrop)
    }
  }, [])

  const accepts = (e: DragEvent) => !disabled && !internalDrag.current && carriesFiles(e.dataTransfer)

  function handleDragEnter(e: DragEvent) {
    if (!accepts(e)) return
    e.preventDefault()
    depth.current += 1
    setDragging(true)
  }

  function handleDragOver(e: DragEvent) {
    if (!accepts(e)) return
    e.preventDefault()
    e.dataTransfer.dropEffect = "copy"
  }

  function handleDragLeave(e: DragEvent) {
    if (!accepts(e)) return
    depth.current = Math.max(0, depth.current - 1)
    if (depth.current === 0) setDragging(false)
  }

  function handleDrop(e: DragEvent) {
    if (!accepts(e)) return
    e.preventDefault()
    depth.current = 0
    setDragging(false)
    const files = Array.from(e.dataTransfer.files)
    if (files.length) onFiles(files)
  }

  return (
    <div
      className={cn("relative", className)}
      onDragEnter={handleDragEnter}
      onDragOver={handleDragOver}
      onDragLeave={handleDragLeave}
      onDrop={handleDrop}
      data-testid="chat-drop-zone"
    >
      {children}
      {dragging && (
        <div
          className="pointer-events-none absolute inset-2 z-50 flex flex-col items-center justify-center gap-2 rounded-xl border-2 border-dashed border-primary bg-background/90 text-primary backdrop-blur-sm"
          data-testid="chat-drop-overlay"
        >
          <Upload className="size-8" aria-hidden="true" />
          <p className="text-base font-medium">Drop files to attach</p>
          <p className="text-xs text-muted-foreground">
            Images, or JSON, CSV, TSV, TXT and MD files
          </p>
        </div>
      )}
    </div>
  )
}
