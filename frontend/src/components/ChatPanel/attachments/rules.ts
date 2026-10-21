/** Limits from the attachments contract (#913); the server checks them again by content. */
export const MAX_ATTACHMENTS = 10
export const MAX_IMAGE_BYTES = 5 * 1024 * 1024
export const MAX_TEXT_BYTES = 2 * 1024 * 1024
export const IMAGE_MAX_EDGE_PX = 1568
/** A downscaled PNG over this is re-encoded lossy; screenshots usually stay well under it. */
export const PNG_KEEP_MAX_BYTES = 1.5 * 1024 * 1024

export const IMAGE_MEDIA_TYPES = ["image/png", "image/jpeg", "image/webp", "image/gif"] as const
const TEXT_EXTENSIONS: Record<string, string> = {
  json: "application/json",
  csv: "text/csv",
  tsv: "text/tab-separated-values",
  txt: "text/plain",
  md: "text/markdown",
}

const IMAGE_EXTENSIONS: Record<string, string> = {
  png: "image/png",
  jpg: "image/jpeg",
  jpeg: "image/jpeg",
  webp: "image/webp",
  gif: "image/gif",
}

export const ACCEPT_ATTRIBUTE = [
  ...IMAGE_MEDIA_TYPES,
  ...Object.keys(TEXT_EXTENSIONS).map((ext) => `.${ext}`),
].join(",")

export type AttachmentKind = "image" | "text"

export type FileCheck =
  | { ok: true; kind: AttachmentKind; mediaType: string }
  | { ok: false; error: string }

function extension(name: string): string {
  const dot = name.lastIndexOf(".")
  return dot === -1 ? "" : name.slice(dot + 1).toLowerCase()
}

/** Type check only: an image's size is checked after it is downscaled. */
export function checkFileType(file: File): FileCheck {
  if ((IMAGE_MEDIA_TYPES as readonly string[]).includes(file.type)) {
    return { ok: true, kind: "image", mediaType: file.type }
  }
  const ext = extension(file.name)
  // Some sources hand over an image with no type; the server checks its bytes anyway.
  if (!file.type && Object.hasOwn(IMAGE_EXTENSIONS, ext)) {
    return { ok: true, kind: "image", mediaType: IMAGE_EXTENSIONS[ext] }
  }
  // Browsers report .md, .tsv and sometimes .csv as "" or vendor types, so the
  // extension decides for text files.
  // Own keys only: "x.constructor" must not match Object.prototype.constructor.
  if (Object.hasOwn(TEXT_EXTENSIONS, ext)) {
    return { ok: true, kind: "text", mediaType: TEXT_EXTENSIONS[ext] }
  }
  return {
    ok: false,
    error: "Unsupported type: attach an image, or a JSON, CSV, TSV, TXT or MD file.",
  }
}

export function sizeError(kind: AttachmentKind, bytes: number): string | null {
  const max = kind === "image" ? MAX_IMAGE_BYTES : MAX_TEXT_BYTES
  if (bytes <= max) return null
  return `Too large: ${kind === "image" ? "images" : "files"} can be up to ${formatBytes(max)}.`
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1).replace(/\.0$/, "")} MB`
}

/** True when a drag or paste carries files rather than text or links. */
export function carriesFiles(dataTransfer: DataTransfer | null): boolean {
  return Boolean(dataTransfer && Array.from(dataTransfer.types).includes("Files"))
}

export { isAttachmentUrl } from "@/api/chatAttachments"

/**
 * Files a paste should attach, or none to let the text paste as usual. Copying
 * cells from a spreadsheet puts both the text and a picture of the cells on the
 * clipboard; the text is what was meant, so images that come with text are left.
 */
export function filesToAttachFromPaste(clipboard: DataTransfer | null): File[] {
  if (!clipboard) return []
  const files = Array.from(clipboard.files)
  if (!files.length) return []
  const onlyImages = files.every((file) => file.type.startsWith("image/"))
  if (onlyImages && clipboard.getData("text/plain").trim()) return []
  return files
}
