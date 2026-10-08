// Thread ids this tab made up for a new chat, which the server has no history for
// until a first message is sent. Anything else (URL, saved id, the list) may have.
const created = new Set<string>()

export function newLocalThreadId(): string {
  const id = crypto.randomUUID()
  created.add(id)
  return id
}

export function isLocalThread(threadId: string): boolean {
  return created.has(threadId)
}

/** Its first message went out, so it now has history to load. */
export function forgetLocalThread(threadId: string): void {
  created.delete(threadId)
}

/** Its first message was refused before the server kept it, so it still has none. */
export function markLocalThread(threadId: string): void {
  created.add(threadId)
}
