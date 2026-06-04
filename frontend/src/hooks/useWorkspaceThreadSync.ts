import { useEffect, useRef } from "react"
import { useLocation, useNavigate, useParams } from "react-router-dom"
import { useAppStore } from "@/store/store"
import { workspacePath } from "@/lib/workspacePath"

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

  // Canonical, pretty chat URL for a (workspaceId, threadId) pair. Resolves the
  // workspace from `domains` so a slug can be derived; degrades to the bare
  // `/workspaces/<uuid>/chat` form when the workspace isn't loaded yet.
  const chatUrl = (workspaceId: string, thread: string | null) => {
    const ws = domains.find((d) => d.id === workspaceId)
    const base = `${pathPrefix}${workspacePath(ws ?? { id: workspaceId })}/chat`
    return thread ? `${base}/${thread}` : base
  }

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

    const target = chatUrl(activeDomainId, threadId || null)

    syncedRef.current = { workspaceId: activeDomainId, threadId: threadId || null }
    navigate(target, { replace: false })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeDomainId, threadId])

  // Canonicalize the address bar: landing on a BARE chat URL
  // (`/workspaces/<uuid>/chat[/<thread>]`) — or any non-pretty variant — should
  // rewrite to the slug form once the workspace is resolvable. This runs after
  // Direction 1 has adopted the URL's workspace into the store, so it relies on
  // `activeDomainId` rather than re-reading params.
  //
  // Loop guard: only rewrite when (a) we're on a chat route, (b) the active
  // workspace is found in `domains` (so a stable slug exists — never rewrite to
  // a different form on the next render), and (c) the canonical URL actually
  // differs from the current pathname. The rewrite is `replace` so it doesn't
  // add a history entry, and matching `domains` means a second pass produces the
  // identical canonical path and the `!==` check short-circuits it.
  useEffect(() => {
    if (!activeDomainId) return
    // Derive the on-route chat thread from the URL params, not the store, so the
    // rewrite preserves exactly what's in the address bar.
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
