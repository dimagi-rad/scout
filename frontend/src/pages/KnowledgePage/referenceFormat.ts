import { downloadBlob } from "@/lib/download"
import {
  REFERENCE_UPLOAD_EXTENSIONS,
  REFERENCE_UPLOAD_MAX_BYTES,
  type KnowledgeEntryItem,
} from "@/store/knowledgeSlice"

export function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

export function formatChars(chars: number): string {
  if (chars < 1000) return `${chars} chars`
  if (chars < 1_000_000) return `${(chars / 1000).toFixed(1)}k chars`
  return `${(chars / 1_000_000).toFixed(1)}M chars`
}

const MIME_TYPES: Record<string, string> = {
  json: "application/json",
  csv: "text/csv",
  tsv: "text/tab-separated-values",
  md: "text/markdown",
  txt: "text/plain",
}

export function referenceFilename(item: Pick<KnowledgeEntryItem, "title" | "source_format">): string {
  // Only characters a file system rejects are replaced, so non-Latin titles survive.
  const base = item.title
    // eslint-disable-next-line no-control-regex
    .replace(/[\\/:*?"<>|\u0000-\u001f]+/g, "_")
    .replace(/^[_\s.]+|[_\s.]+$/g, "")
  return `${base || "document"}.${item.source_format || "txt"}`
}

/** The whole document: the item itself unless the list only holds a preview of it. */
export async function fullReference(
  item: KnowledgeEntryItem,
  fetchItem: (id: string) => Promise<KnowledgeEntryItem | null>,
): Promise<KnowledgeEntryItem | null> {
  return item.content_truncated ? fetchItem(item.id) : item
}

export function downloadText(
  item: Pick<KnowledgeEntryItem, "title" | "source_format" | "content">,
): void {
  const type = MIME_TYPES[item.source_format || "txt"] ?? "text/plain"
  downloadBlob(referenceFilename(item), new Blob([item.content], { type: `${type};charset=utf-8` }))
}

export function fileProblem(file: File): string | null {
  const name = file.name.toLowerCase()
  if (!REFERENCE_UPLOAD_EXTENSIONS.some((ext) => name.endsWith(ext))) {
    return `Upload a ${REFERENCE_UPLOAD_EXTENSIONS.join(", ")} file.`
  }
  if (file.size > REFERENCE_UPLOAD_MAX_BYTES) {
    return (
      `This file is ${file.size.toLocaleString("en-US")} bytes; the limit is ` +
      `${REFERENCE_UPLOAD_MAX_BYTES.toLocaleString("en-US")} bytes (${formatSize(REFERENCE_UPLOAD_MAX_BYTES)}).`
    )
  }
  return null
}
