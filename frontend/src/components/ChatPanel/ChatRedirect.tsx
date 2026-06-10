import { useEffect } from "react"
import { Navigate, useLocation } from "react-router-dom"
import { useAppStore } from "@/store/store"
import { workspacePath } from "@/lib/workspacePath"
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
  const domains = useAppStore((s) => s.domains)
  const domainsStatus = useAppStore((s) => s.domainsStatus)
  const fetchDomains = useAppStore((s) => s.domainActions.fetchDomains)

  // Domains may not have been fetched yet if the chat route is the entry point.
  useEffect(() => {
    if (domainsStatus === "idle") fetchDomains()
  }, [domainsStatus, fetchDomains])

  if (activeDomainId) {
    // Resolve the last thread the user viewed *in this workspace* (persisted in
    // localStorage by ChatPanel, only after a successful messages load). We do
    // NOT fall back to the store's `threadId` here: that value can belong to a
    // different workspace (e.g. right after a workspace switch), and grafting it
    // onto this workspace's URL is exactly the bug that produced "Thread not
    // found". When there's no saved thread, redirect to the bare chat URL and
    // let ChatPanel start a fresh thread.
    const resolvedThreadId = readSavedThreadId(activeDomainId)
    const activeWorkspace = domains.find((d) => d.id === activeDomainId)
    const chatBase = `${pathPrefix}${workspacePath(activeWorkspace ?? { id: activeDomainId })}/chat`
    const target = resolvedThreadId ? `${chatBase}/${resolvedThreadId}` : chatBase
    return <Navigate to={target} replace />
  }

  return <ChatPanel />
}
