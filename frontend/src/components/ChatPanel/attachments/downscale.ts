import { IMAGE_MAX_EDGE_PX, MAX_IMAGE_BYTES, PNG_KEEP_MAX_BYTES } from "./rules"

const EXTENSIONS: Record<string, string> = {
  "image/png": "png",
  "image/jpeg": "jpg",
  "image/webp": "webp",
}

function renamed(name: string, mediaType: string): string {
  const ext = EXTENSIONS[mediaType]
  if (!ext) return name
  const dot = name.lastIndexOf(".")
  return `${dot === -1 ? name : name.slice(0, dot)}.${ext}`
}

function encode(canvas: HTMLCanvasElement, type: string): Promise<Blob | null> {
  return new Promise((resolve) => canvas.toBlob(resolve, type, 0.9))
}

/**
 * Shrinks an image to IMAGE_MAX_EDGE_PX on its long edge, the most the model reads
 * at full detail, so screenshots upload fast and stay under the size limit. A PNG
 * stays a PNG (sharp text) unless it is still large, then becomes WebP, or JPEG
 * where the browser can't encode WebP (Safari's toBlob falls back to PNG). A WebP
 * the browser can't re-encode becomes JPEG when it is opaque.
 * GIFs pass through untouched: a canvas would keep only the first frame.
 */
export async function downscaleImage(file: File): Promise<File> {
  if (file.type === "image/gif") return file
  const bitmap = await createImageBitmap(file)
  try {
    const longEdge = Math.max(bitmap.width, bitmap.height)
    const tooHeavy = file.type === "image/png" ? file.size > PNG_KEEP_MAX_BYTES : file.size > MAX_IMAGE_BYTES
    if (longEdge <= IMAGE_MAX_EDGE_PX && !tooHeavy) return file
    const scale = Math.min(1, IMAGE_MAX_EDGE_PX / longEdge)
    const canvas = document.createElement("canvas")
    canvas.width = Math.max(1, Math.round(bitmap.width * scale))
    canvas.height = Math.max(1, Math.round(bitmap.height * scale))
    const context = canvas.getContext("2d")
    if (!context) return file
    context.drawImage(bitmap, 0, 0, canvas.width, canvas.height)

    const toJpeg = () => {
      // JPEG has no alpha: transparent pixels would turn black.
      context.globalCompositeOperation = "destination-over"
      context.fillStyle = "#fff"
      context.fillRect(0, 0, canvas.width, canvas.height)
      return encode(canvas, "image/jpeg")
    }

    let blob = await encode(canvas, file.type)
    // An encoder the browser lacks (Safari: WebP) silently gives a PNG instead.
    if (blob && blob.type !== file.type && !hasTransparency(context, canvas.width, canvas.height)) {
      blob = await toJpeg()
    } else if (blob && file.type === "image/png" && blob.size > PNG_KEEP_MAX_BYTES) {
      const webp = await encode(canvas, "image/webp")
      blob = webp?.type === "image/webp" ? webp : await toJpeg()
    }
    if (!blob || (scale === 1 && blob.size >= file.size)) return file
    return new File([blob], renamed(file.name, blob.type), { type: blob.type })
  } finally {
    bitmap.close()
  }
}

function hasTransparency(context: CanvasRenderingContext2D, width: number, height: number): boolean {
  const { data } = context.getImageData(0, 0, width, height)
  for (let i = 3; i < data.length; i += 4) if (data[i] < 255) return true
  return false
}
