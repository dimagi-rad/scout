// Mirrors short_thread_title in apps/chat/titles.py.
const THREAD_TITLE_PREVIEW_CHARS = 200

export function shortThreadTitle(title: string): string {
  const clean = title.trim()
  if (clean.length > THREAD_TITLE_PREVIEW_CHARS) {
    return `${clean.slice(0, THREAD_TITLE_PREVIEW_CHARS).trimEnd()}...`
  }
  return clean
}
