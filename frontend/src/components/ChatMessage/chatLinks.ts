export function internalChatPath(href: string | undefined, origin: string): string | null {
  if (!href?.startsWith("/")) return null
  try {
    const resolved = new URL(href, origin)
    if (resolved.origin !== origin) return null
    return `${resolved.pathname}${resolved.search}${resolved.hash}`
  } catch {
    return null
  }
}
