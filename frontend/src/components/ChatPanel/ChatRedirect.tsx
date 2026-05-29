import { useEffect } from "react"
import { Navigate, useLocation } from "react-router-dom"
import { useAppStore } from "@/store/store"
import { ChatPanel } from "./ChatPanel"
import { readSavedThreadId } from "./threadStorage"

/**
 * Entry point for the bare chat routes (`/` and `/chat`). Once the active
 * workspace is known, redirects to the canonical, bookmarkable chat URL
 * (`/workspaces/:workspaceId/chat[/:threadId]`). While domains are still
 * loading — or the user has no workspace yet — it renders the chat panel
 * directly so the existing empty/loading states still show.
 */
export function ChatRedirect() {
  const location = useLocation()
  const pathPrefix = location.pathname.startsWith("/embed") ? "/embed" : ""
  const activeDomainId = useAppStore((s) => s.activeDomainId)
  const threadId = useAppStore((s) => s.threadId)
  const domainsStatus = useAppStore((s) => s.domainsStatus)
  const fetchDomains = useAppStore((s) => s.domainActions.fetchDomains)

  // Domains may not have been fetched yet if the chat route is the entry point.
  useEffect(() => {
    if (domainsStatus === "idle") fetchDomains()
  }, [domainsStatus, fetchDomains])

  if (activeDomainId) {
    // Prefer the last thread the user viewed in this workspace (persisted in
    // localStorage by ChatPanel); fall back to the store's current threadId.
    const resolvedThreadId = readSavedThreadId(activeDomainId) || threadId
    const target = resolvedThreadId
      ? `${pathPrefix}/workspaces/${activeDomainId}/chat/${resolvedThreadId}`
      : `${pathPrefix}/workspaces/${activeDomainId}/chat`
    return <Navigate to={target} replace />
  }

  return <ChatPanel />
}
