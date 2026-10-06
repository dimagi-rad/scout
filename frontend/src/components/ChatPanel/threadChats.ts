import type { Chat } from "@ai-sdk/react"
import type { UIMessage } from "ai"
import { useEffect, useState } from "react"

export function isChatRunning(chat: Chat<UIMessage>): boolean {
  return chat.status === "submitted" || chat.status === "streaming"
}

export function threadChatKey(workspaceId: string | null, threadId: string): string {
  return `${workspaceId ?? ""}\u0000${threadId}`
}

/**
 * The Chat for the shown thread, one per (workspace, thread), so a turn streams
 * only into the thread it was sent from and keeps going while another is shown
 * (#847). A left chat is dropped once it is not running; its thread reloads from
 * the server when shown again.
 */
export function useThreadChat(
  workspaceId: string | null,
  threadId: string,
  create: (workspaceId: string | null, threadId: string) => Chat<UIMessage>,
): Chat<UIMessage> {
  // A cache, not state: creating a chat on first render must not re-render.
  const [chats] = useState(() => new Map<string, Chat<UIMessage>>())
  const key = threadChatKey(workspaceId, threadId)
  let chat = chats.get(key)
  if (!chat) {
    chat = create(workspaceId, threadId)
    chats.set(key, chat)
  }

  useEffect(() => {
    for (const [other, otherChat] of chats) {
      if (other !== key && !isChatRunning(otherChat)) chats.delete(other)
    }
  }, [chats, key])

  return chat
}
