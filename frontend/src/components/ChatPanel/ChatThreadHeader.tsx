import { useEffect, useRef, useState } from "react"
import { Check, FileText, Loader2, PanelsTopLeft, Pencil } from "lucide-react"

import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"

export type ThreadPanelMode = "files" | "canvas"

const THREAD_TITLE_PREVIEW_CHARS = 200

interface ChatThreadHeaderProps {
  title: string
  titleIsCustom: boolean
  panelOpen: boolean
  panelMode: ThreadPanelMode
  onTitleChange: (title: string) => Promise<void> | void
  onOpenFiles: () => void
  onOpenCanvas: () => void
}

export function ChatThreadHeader({
  title,
  titleIsCustom,
  panelOpen,
  panelMode,
  onTitleChange,
  onOpenFiles,
  onOpenCanvas,
}: ChatThreadHeaderProps) {
  const displayTitle = shortThreadTitle(title)
  const isUntitledFallback = !titleIsCustom && displayTitle === "Untitled"
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(displayTitle)
  const [saveState, setSaveState] = useState<"idle" | "saving" | "saved" | "error">("idle")
  const inputRef = useRef<HTMLInputElement>(null)
  const draftRef = useRef(displayTitle)

  useEffect(() => {
    if (!editing) {
      setDraft(displayTitle)
      draftRef.current = displayTitle
    }
  }, [displayTitle, editing])

  useEffect(() => {
    if (editing) inputRef.current?.select()
  }, [editing])

  async function commitTitle(value = inputRef.current?.value ?? draftRef.current) {
    const cleanDraft = value.trim()
    const nextDisplayTitle = cleanDraft || "Untitled"
    setDraft(nextDisplayTitle)
    setEditing(false)
    if (!cleanDraft) {
      if (!titleIsCustom && displayTitle === "Untitled") return
    } else if (titleIsCustom && cleanDraft === title.trim()) {
      return
    }
    setSaveState("saving")
    try {
      await onTitleChange(cleanDraft)
      setSaveState("saved")
      window.setTimeout(() => setSaveState("idle"), 1200)
    } catch {
      setSaveState("error")
    }
  }

  function cancelEdit() {
    setDraft(displayTitle)
    setEditing(false)
  }

  return (
    <header className="flex h-12 shrink-0 items-center justify-between gap-3 border-b bg-background px-4">
      <div className="flex min-w-0 items-center gap-2">
        {editing ? (
          <form
            onSubmit={(event) => {
              event.preventDefault()
              void commitTitle()
            }}
            className="min-w-0"
          >
            <input
              ref={inputRef}
              value={draft}
              onChange={(event) => {
                draftRef.current = event.currentTarget.value
                setDraft(event.currentTarget.value)
              }}
              onBlur={() => void commitTitle()}
              onKeyDown={(event) => {
                if (event.key === "Escape") {
                  event.preventDefault()
                  cancelEdit()
                }
              }}
              className="h-8 min-w-0 max-w-[34rem] rounded-md border bg-background px-2 text-sm font-medium outline-none focus-visible:border-ring focus-visible:ring-2 focus-visible:ring-ring/30"
              aria-label="Thread title"
            />
          </form>
        ) : (
          <button
            type="button"
            onClick={() => setEditing(true)}
            className="group flex min-w-0 items-center gap-2 rounded-md px-1.5 py-1 text-left hover:bg-accent"
            aria-label="Rename thread"
          >
            <span
              className={cn(
                "truncate text-sm font-medium",
                isUntitledFallback ? "text-muted-foreground" : "text-foreground",
              )}
            >
              {displayTitle}
            </span>
            <Pencil className="h-3.5 w-3.5 shrink-0 text-muted-foreground opacity-0 transition-opacity group-hover:opacity-100" />
          </button>
        )}
        {saveState === "saving" && <Loader2 className="h-3.5 w-3.5 animate-spin text-muted-foreground" />}
        {saveState === "saved" && <Check className="h-3.5 w-3.5 text-emerald-600" />}
        {saveState === "error" && (
          <span className="text-xs text-destructive">Rename failed</span>
        )}
      </div>

      <div className="flex shrink-0 items-center gap-1">
        <Button
          type="button"
          variant={panelOpen && panelMode === "files" ? "secondary" : "ghost"}
          size="icon-sm"
          onClick={onOpenFiles}
          title="Files"
          aria-label="Files"
          className={cn(panelOpen && panelMode === "files" && "bg-accent")}
        >
          <FileText className="h-4 w-4" />
        </Button>
        <Button
          type="button"
          variant={panelOpen && panelMode === "canvas" ? "secondary" : "ghost"}
          size="icon-sm"
          onClick={onOpenCanvas}
          title="Canvas"
          aria-label="Canvas"
          className={cn(panelOpen && panelMode === "canvas" && "bg-accent")}
        >
          <PanelsTopLeft className="h-4 w-4" />
        </Button>
      </div>
    </header>
  )
}

function shortThreadTitle(title: string): string {
  const clean = title.trim()
  if (!clean) return "Untitled"
  if (clean.length > THREAD_TITLE_PREVIEW_CHARS) {
    return `${clean.slice(0, THREAD_TITLE_PREVIEW_CHARS).trimEnd()}...`
  }
  return clean
}
