import { useEffect, useRef } from "react"
import { useNavigate, useParams } from "react-router-dom"
import { useAppStore } from "@/store/store"

/**
 * Two-way bridge between the chat URL (`/workspaces/:workspaceId/chat/:threadId`)
 * and the zustand store (`activeDomainId` / `threadId`).
 *
 * Direction 1 — URL → store: when the user navigates directly (bookmark, paste,
 * back/forward), the params drive the store so the correct workspace + thread
 * are restored.
 *
 * Direction 2 — store → URL: when the store changes from in-app actions (e.g.
 * the workspace switcher or starting a new thread), the URL is updated so the
 * current view is bookmarkable.
 *
 * A guard ref records the last (workspaceId, threadId) pair we reconciled so we
 * never bounce an update back to its origin and never create a navigation loop.
 *
 * @param pathPrefix "" for the main app, "/embed" for the embedded app.
 */
export function useWorkspaceThreadSync(pathPrefix: string) {
  const navigate = useNavigate()
  const { workspaceId: urlWorkspaceId, threadId: urlThreadId } = useParams<{
    workspaceId: string
    threadId: string
  }>()

  const activeDomainId = useAppStore((s) => s.activeDomainId)
  const threadId = useAppStore((s) => s.threadId)
  const domainsStatus = useAppStore((s) => s.domainsStatus)
  const domains = useAppStore((s) => s.domains)
  const setActiveDomain = useAppStore((s) => s.domainActions.setActiveDomain)
  const selectThread = useAppStore((s) => s.uiActions.selectThread)

  // Last pair we reconciled, in either direction. Prevents ping-pong loops.
  const syncedRef = useRef<{ workspaceId: string | null; threadId: string | null }>({
    workspaceId: null,
    threadId: null,
  })

  // Direction 1: URL → store
  useEffect(() => {
    if (!urlWorkspaceId) return
    if (
      syncedRef.current.workspaceId === urlWorkspaceId &&
      syncedRef.current.threadId === (urlThreadId ?? null)
    ) {
      return
    }

    // Only adopt a workspace from the URL once domains have loaded and the id is
    // valid for this user — otherwise leave the store alone (the redirect logic
    // below or fetchDomains will pick a sensible default).
    if (domainsStatus === "loaded" && !domains.some((d) => d.id === urlWorkspaceId)) {
      return
    }

    if (urlWorkspaceId !== activeDomainId) {
      setActiveDomain(urlWorkspaceId)
    }
    if (urlThreadId && urlThreadId !== threadId) {
      void selectThread(urlThreadId)
    }

    syncedRef.current = { workspaceId: urlWorkspaceId, threadId: urlThreadId ?? null }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [urlWorkspaceId, urlThreadId, domainsStatus, domains])

  // Direction 2: store → URL
  useEffect(() => {
    if (!activeDomainId) return
    if (
      syncedRef.current.workspaceId === activeDomainId &&
      syncedRef.current.threadId === (threadId || null)
    ) {
      return
    }

    const target = threadId
      ? `${pathPrefix}/workspaces/${activeDomainId}/chat/${threadId}`
      : `${pathPrefix}/workspaces/${activeDomainId}/chat`

    syncedRef.current = { workspaceId: activeDomainId, threadId: threadId || null }
    navigate(target, { replace: false })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeDomainId, threadId])
}
