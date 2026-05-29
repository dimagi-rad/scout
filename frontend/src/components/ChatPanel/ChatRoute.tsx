import { useLocation } from "react-router-dom"
import { ChatPanel } from "./ChatPanel"
import { useWorkspaceThreadSync } from "@/hooks/useWorkspaceThreadSync"

/**
 * Chat route wrapper. Keeps the URL (`/workspaces/:workspaceId/chat/:threadId`)
 * and the store's active workspace + thread in sync, then renders the chat UI.
 */
export function ChatRoute() {
  const location = useLocation()
  const pathPrefix = location.pathname.startsWith("/embed") ? "/embed" : ""
  useWorkspaceThreadSync(pathPrefix)
  return <ChatPanel />
}
