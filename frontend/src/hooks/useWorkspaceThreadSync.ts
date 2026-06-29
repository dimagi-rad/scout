import { useEffect, useRef } from "react"
import { useLocation, useNavigate, useParams } from "react-router-dom"
import { useAppStore } from "@/store/store"
import { workspacePath } from "@/lib/workspacePath"

/**
 * Two-way bridge between the chat URL (`/workspaces/:workspaceId/chat/:threadId`)
 * and the zustand store (`activeDomainId` / `threadId`).
 *
 * Direction 1 — URL → store: direct navigation (bookmark, paste, back/forward)
 * drives the store. Direction 2 — store → URL: in-app actions (workspace
 * switcher, new thread) update the URL so the view stays bookmarkable.
 *
 * A guard ref records the last reconciled (workspaceId, threadId) pair so an
 * update never bounces back to its origin or creates a navigation loop.
 *
 * @param pathPrefix "" for the main app, "/embed" for the embedded app.
 */
export function useWorkspaceThreadSync(pathPrefix: string) {
  const navigate = useNavigate()
  const location = useLocation()
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

  // Canonical pretty chat URL; degrades to the bare `/workspaces/<uuid>/chat`
  // form when the workspace isn't loaded yet (no slug derivable).
  const chatUrl = (workspaceId: string, thread: string | null) => {
    const ws = domains.find((d) => d.id === workspaceId)
    const base = `${pathPrefix}${workspacePath(ws ?? { id: workspaceId })}/chat`
    return thread ? `${base}/${thread}` : base
  }

  // Last reconciled pair, either direction. Prevents ping-pong loops.
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

    // Only adopt a URL workspace once domains have loaded and the id is valid
    // for this user; otherwise leave the store for fetchDomains to default.
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

    const target = chatUrl(activeDomainId, threadId || null)

    syncedRef.current = { workspaceId: activeDomainId, threadId: threadId || null }
    navigate(target, { replace: false })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeDomainId, threadId])

  // Canonicalize the address bar: rewrite a bare/non-pretty chat URL to the slug
  // form once the workspace resolves. Loop guard: only rewrite when on a chat
  // route, the workspace exists in `domains` (so a stable slug exists and a
  // second pass yields the identical path), and the canonical URL differs from
  // the current pathname. `replace` avoids adding a history entry.
  useEffect(() => {
    if (!activeDomainId) return
    // Derive the thread from URL params, not the store, so the rewrite preserves
    // exactly what's in the address bar.
    const onChatRoute = urlWorkspaceId === activeDomainId
    if (!onChatRoute) return
    if (!domains.some((d) => d.id === activeDomainId)) return

    const canonical = chatUrl(activeDomainId, urlThreadId ?? null)
    if (canonical !== location.pathname) {
      navigate(canonical, { replace: true })
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeDomainId, urlWorkspaceId, urlThreadId, domains, location.pathname])
}
