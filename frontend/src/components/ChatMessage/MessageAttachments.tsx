import type { FileUIPart } from "ai"
import { FileText, ImageOff } from "lucide-react"
import { useState } from "react"

import { withBasePath } from "@/config"
import { isAttachmentUrl } from "@/api/chatAttachments"
import { cn } from "@/lib/utils"

const TYPE_LABELS: Record<string, string> = {
  "application/json": "JSON",
  "text/csv": "CSV",
  "text/tab-separated-values": "TSV",
  "text/plain": "Text",
  "text/markdown": "Markdown",
}

/** History parts come from the server; a missing type must not take the transcript down. */
function mediaTypeOf(part: FileUIPart): string {
  return typeof part.mediaType === "string" ? part.mediaType : ""
}

function typeLabel(part: FileUIPart): string {
  const base = mediaTypeOf(part).split(";")[0]
  if (Object.hasOwn(TYPE_LABELS, base)) return TYPE_LABELS[base]
  const name = part.filename ?? ""
  const dot = name.lastIndexOf(".")
  // No extension, or a dotfile such as ".gitignore".
  return dot > 0 && dot < name.length - 1 ? name.slice(dot + 1).toUpperCase() : "File"
}

const CHIP_CLASS =
  "flex max-w-64 items-center gap-2 rounded-lg border bg-muted/50 py-1 pl-1 pr-3 text-xs"

function Chip({ name, detail, broken = false }: { name: string; detail: string; broken?: boolean }) {
  const Icon = broken ? ImageOff : FileText
  return (
    <>
      <span className="flex size-8 shrink-0 items-center justify-center rounded bg-background">
        <Icon className="size-4 text-muted-foreground" aria-hidden="true" />
      </span>
      <span className="min-w-0">
        <span className="block truncate font-medium">{name}</span>
        <span className="block text-muted-foreground">{detail}</span>
      </span>
    </>
  )
}

function MessageAttachment({ part }: { part: FileUIPart }) {
  // The download can fail after the send: access to the workspace lost (403), or
  // the attachment gone. A broken image then reads as a named chip.
  const [broken, setBroken] = useState(false)
  const name = part.filename || "attachment"
  // Only our own attachment urls are fetched or linked; anything else shows as a name.
  const href = isAttachmentUrl(part.url) ? withBasePath(part.url) : null
  const image = mediaTypeOf(part).startsWith("image/")
  if (href && image && !broken) {
    return (
      <a
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
          onError={() => setBroken(true)}
          className="max-h-48 max-w-64 object-contain"
        />
      </a>
    )
  }
  if (href && !broken) {
    return (
      <a
        href={href}
        download={name}
        className={cn(CHIP_CLASS, "hover:bg-muted")}
        title={`Download ${name}`}
        data-testid="chat-message-attachment"
        data-kind="file"
      >
        <Chip name={name} detail={typeLabel(part)} />
      </a>
    )
  }
  return (
    <span
      className={CHIP_CLASS}
      title={broken ? `${name} can't be shown` : undefined}
      data-testid="chat-message-attachment"
      data-kind={broken ? "unavailable" : "file"}
    >
      <Chip name={name} detail={broken ? "Image unavailable" : typeLabel(part)} broken={broken} />
    </span>
  )
}

/** The files a user message carried: image thumbnails that open full size, and file chips. */
export function MessageAttachments({ parts, alignEnd }: { parts: FileUIPart[]; alignEnd: boolean }) {
  return (
    <div className={cn("mb-1 flex flex-wrap items-start gap-2", alignEnd && "justify-end")}>
      {parts.map((part, i) => (
        <MessageAttachment key={`${i}:${part.url}`} part={part} />
      ))}
    </div>
  )
}
