import { useRef } from "react"
import { AlertCircle, FileText, Loader2, Paperclip, X } from "lucide-react"

import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"
import { ACCEPT_ATTRIBUTE, formatBytes } from "./rules"
import type { ComposerAttachment } from "./useChatAttachments"

export function AttachmentTray({
  items,
  notice,
  onRemove,
}: {
  items: ComposerAttachment[]
  notice?: string | null
  onRemove: (localId: string) => void
}) {
  if (!items.length && !notice) return null
  return (
    <div className="space-y-1">
      {items.length > 0 && (
        <ul className="flex flex-wrap gap-2" aria-label="Attached files">
          {items.map((item, index) => (
            <AttachmentChip key={item.localId} item={item} index={index} onRemove={onRemove} />
          ))}
        </ul>
      )}
      {notice && (
        <p className="text-xs text-destructive" data-testid="chat-attachment-notice">
          {notice}
        </p>
      )}
    </div>
  )
}

function statusText(item: ComposerAttachment): string | undefined {
  switch (item.status) {
    case "error":
      return item.error
    case "processing":
      return "Preparing…"
    case "uploading":
      return `Uploading ${Math.round(item.progress * 100)}%`
    case "ready":
      // A file handed back from a refused send has no size on hand.
      return item.size ? formatBytes(item.size) : "Attached"
  }
}

function AttachmentChip({
  item,
  index,
  onRemove,
}: {
  item: ComposerAttachment
  index: number
  onRemove: (localId: string) => void
}) {
  const failed = item.status === "error"
  const working = item.status === "processing" || item.status === "uploading"
  const status = statusText(item)
  return (
    <li
      className={cn(
        "relative flex max-w-64 items-center gap-2 overflow-hidden rounded-md border bg-muted/50 py-1 pl-1 pr-7 text-xs",
        failed && "border-destructive/60 bg-destructive/5",
      )}
      data-testid={`chat-attachment-chip-${index}`}
      data-status={item.status}
      title={failed ? `${item.filename}: ${item.error}` : item.filename}
    >
      <span className="flex size-9 shrink-0 items-center justify-center overflow-hidden rounded bg-background">
        {item.previewUrl ? (
          <img src={item.previewUrl} alt="" className="size-full object-cover" draggable={false} />
        ) : failed ? (
          <AlertCircle className="size-4 text-destructive" aria-hidden="true" />
        ) : (
          <FileText className="size-4 text-muted-foreground" aria-hidden="true" />
        )}
      </span>
      <span className="min-w-0">
        <span className="block truncate font-medium">{item.filename}</span>
        <span
          className={cn(
            "flex items-center gap-1",
            failed ? "line-clamp-2 text-destructive" : "text-muted-foreground",
          )}
        >
          {working && <Loader2 className="size-3 shrink-0 animate-spin" aria-hidden="true" />}
          <span className={failed ? "line-clamp-2" : "truncate"}>{status}</span>
        </span>
      </span>
      <button
        type="button"
        onClick={() => onRemove(item.localId)}
        aria-label={`Remove ${item.filename}`}
        className="absolute right-1 top-1 rounded p-0.5 text-muted-foreground hover:bg-background hover:text-foreground"
        data-testid={`chat-attachment-remove-${index}`}
      >
        <X className="size-3.5" aria-hidden="true" />
      </button>
      {/* Only the outcome is announced, not every progress tick. */}
      <span className="sr-only" aria-live="polite">
        {failed ? `${item.filename}: ${item.error}` : item.status === "ready" ? `${item.filename} attached` : ""}
      </span>
      {item.status === "uploading" && (
        <span
          className="absolute inset-x-0 bottom-0 h-0.5 bg-primary transition-[width]"
          style={{ width: `${Math.round(item.progress * 100)}%` }}
          aria-hidden="true"
        />
      )}
    </li>
  )
}

export function AttachButton({
  onFiles,
  disabled = false,
  className,
}: {
  onFiles: (files: File[]) => void
  disabled?: boolean
  className?: string
}) {
  const inputRef = useRef<HTMLInputElement>(null)
  return (
    <>
      <Button
        type="button"
        variant="ghost"
        size="icon"
        onClick={() => inputRef.current?.click()}
        disabled={disabled}
        aria-label="Attach files"
        title="Attach images or data files (or drop / paste them)"
        className={className}
        data-testid="chat-attach"
      >
        <Paperclip className="size-4" aria-hidden="true" />
      </Button>
      <input
        ref={inputRef}
        type="file"
        multiple
        accept={ACCEPT_ATTRIBUTE}
        className="hidden"
        tabIndex={-1}
        data-testid="chat-attach-input"
        onChange={(e) => {
          const files = Array.from(e.target.files ?? [])
          // Cleared so picking the same file again still fires change.
          e.target.value = ""
          if (files.length) onFiles(files)
        }}
      />
    </>
  )
}
