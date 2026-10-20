import type { FileUIPart } from "ai"
import { FileText } from "lucide-react"

import { withBasePath } from "@/config"
import { isAttachmentUrl } from "@/components/ChatPanel/attachments/rules"
import { cn } from "@/lib/utils"

const TYPE_LABELS: Record<string, string> = {
  "application/json": "JSON",
  "text/csv": "CSV",
  "text/tab-separated-values": "TSV",
  "text/plain": "Text",
  "text/markdown": "Markdown",
}

function typeLabel(part: FileUIPart): string {
  const base = part.mediaType.split(";")[0]
  if (Object.hasOwn(TYPE_LABELS, base)) return TYPE_LABELS[base]
  const ext = part.filename?.split(".").pop()
  return ext && ext !== part.filename ? ext.toUpperCase() : "File"
}

/** The files a user message carried: image thumbnails that open full size, and file chips. */
export function MessageAttachments({ parts, alignEnd }: { parts: FileUIPart[]; alignEnd: boolean }) {
  return (
    <div className={cn("mb-1 flex flex-wrap items-start gap-2", alignEnd && "justify-end")}>
      {parts.map((part, i) => {
        const name = part.filename || "attachment"
        // Only our own attachment urls are fetched or linked; anything else shows as a name.
        const href = isAttachmentUrl(part.url) ? withBasePath(part.url) : null
        if (href && part.mediaType.startsWith("image/")) {
          return (
            <a
              key={i}
              href={href}
              target="_blank"
              rel="noopener noreferrer"
              className="block overflow-hidden rounded-lg border bg-muted"
              title={`Open ${name}`}
              data-testid="chat-message-attachment"
              data-kind="image"
            >
              <img
                src={href}
                alt={name}
                loading="lazy"
                draggable={false}
                className="max-h-48 max-w-64 object-contain"
              />
            </a>
          )
        }
        const chip = (
          <>
            <span className="flex size-8 shrink-0 items-center justify-center rounded bg-background">
              <FileText className="size-4 text-muted-foreground" aria-hidden="true" />
            </span>
            <span className="min-w-0">
              <span className="block truncate font-medium">{name}</span>
              <span className="block text-muted-foreground">{typeLabel(part)}</span>
            </span>
          </>
        )
        const chipClass =
          "flex max-w-64 items-center gap-2 rounded-lg border bg-muted/50 py-1 pl-1 pr-3 text-xs"
        return href ? (
          <a
            key={i}
            href={href}
            download={name}
            className={cn(chipClass, "hover:bg-muted")}
            title={`Download ${name}`}
            data-testid="chat-message-attachment"
            data-kind="file"
          >
            {chip}
          </a>
        ) : (
          <span key={i} className={chipClass} data-testid="chat-message-attachment" data-kind="file">
            {chip}
          </span>
        )
      })}
    </div>
  )
}
