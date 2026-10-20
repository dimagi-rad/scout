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
  const base = item.title.replace(/[^\w .-]+/g, "_").trim() || "document"
  return `${base}.${item.source_format || "txt"}`
}

export function downloadText(
  item: Pick<KnowledgeEntryItem, "title" | "source_format" | "content">,
): void {
  const type = MIME_TYPES[item.source_format || "txt"] ?? "text/plain"
  const url = URL.createObjectURL(new Blob([item.content], { type: `${type};charset=utf-8` }))
  const a = document.createElement("a")
  a.href = url
  a.download = referenceFilename(item)
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  URL.revokeObjectURL(url)
}

export function fileProblem(file: File): string | null {
  const name = file.name.toLowerCase()
  if (!REFERENCE_UPLOAD_EXTENSIONS.some((ext) => name.endsWith(ext))) {
    return `Upload a ${REFERENCE_UPLOAD_EXTENSIONS.join(", ")} file.`
  }
  if (file.size > REFERENCE_UPLOAD_MAX_BYTES) {
    return `This file is ${formatSize(file.size)}; the limit is ${formatSize(REFERENCE_UPLOAD_MAX_BYTES)}.`
  }
  return null
}
