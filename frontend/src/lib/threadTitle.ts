// Mirrors short_thread_title in apps/chat/titles.py, which counts code points.
const THREAD_TITLE_PREVIEW_CHARS = 200

export function shortThreadTitle(title: string): string {
  const chars = Array.from(title.trim())
  if (chars.length > THREAD_TITLE_PREVIEW_CHARS) {
    return `${chars.slice(0, THREAD_TITLE_PREVIEW_CHARS).join("").trimEnd()}...`
  }
  return chars.join("")
}
