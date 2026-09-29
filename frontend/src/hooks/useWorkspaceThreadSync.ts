import { useEffect, useRef, useState } from "react"
import { useLocation, useNavigate, useParams } from "react-router-dom"
import { useAppStore } from "@/store/store"
import { recordWorkspaceUse } from "@/lib/recentWorkspaces"
import { workspacePath } from "@/lib/workspacePath"

const URL_WORKSPACE_RECHECK_TIMEOUT_MS = 5_000

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
  const revalidateDomains = useAppStore((s) => s.domainActions.revalidateDomains)
  const selectThread = useAppStore((s) => s.uiActions.selectThread)

  // Canonical pretty chat URL; degrades to the bare `/workspaces/<uuid>/chat`
  // form when the workspace isn't loaded yet (no slug derivable).
  const chatUrl = (workspaceId: string, thread: string | null, list = domains) => {
    const ws = list.find((d) => d.id === workspaceId)
    const base = `${pathPrefix}${workspacePath(ws ?? { id: workspaceId })}/chat`
    return thread ? `${base}/${thread}` : base
  }

  // Last reconciled pair, either direction. Prevents ping-pong loops.
  const syncedRef = useRef<{ workspaceId: string | null; threadId: string | null }>({
    workspaceId: null,
    threadId: null,
  })

  // A deep link to a workspace missing from the list may be one you were added
  // to after the list loaded (#355). Refetch once before giving up on it, and
  // hold store → URL meanwhile so the default workspace doesn't replace the link.
  const [recheckedWorkspaceId, setRecheckedWorkspaceId] = useState<string | null>(null)
  // Each visit to a link gets its own recheck, so A → B → A looks again.
  const [recheckVisit, setRecheckVisit] = useState(urlWorkspaceId)
  if (recheckVisit !== urlWorkspaceId) {
    setRecheckVisit(urlWorkspaceId)
    setRecheckedWorkspaceId(null)
  }
  const awaitingUrlWorkspace =
    !!urlWorkspaceId &&
    domainsStatus === "loaded" &&
    !domains.some((d) => d.id === urlWorkspaceId) &&
    recheckedWorkspaceId !== urlWorkspaceId

  const mountedRef = useRef(true)
  useEffect(() => {
    mountedRef.current = true
    return () => { mountedRef.current = false }
  }, [])
  const currentUrlRef = useRef({ workspaceId: urlWorkspaceId, threadId: urlThreadId ?? null })
  useEffect(() => {
    currentUrlRef.current = { workspaceId: urlWorkspaceId, threadId: urlThreadId ?? null }
  }, [urlWorkspaceId, urlThreadId])

  useEffect(() => {
    if (!awaitingUrlWorkspace || !urlWorkspaceId) return
    const heldActiveDomainId = useAppStore.getState().activeDomainId
    let settled = false
    const giveUp = () => {
      settled = true
      clearTimeout(timer)
      unsubscribe()
      setRecheckedWorkspaceId(urlWorkspaceId)
    }
    let fallback: {
      activeDomainId: string | null
      threadId: string
      linkThreadId: string | null
      at: number
    } | null = null
    // The API client has no timeout; a stalled request must not freeze store → URL sync.
    const timer = setTimeout(() => {
      if (settled) return
      const { activeDomainId: fallbackDomainId, threadId: fallbackThreadId } = useAppStore.getState()
      fallback = {
        activeDomainId: fallbackDomainId,
        threadId: fallbackThreadId,
        linkThreadId: currentUrlRef.current.threadId,
        at: Date.now(),
      }
      giveUp()
    }, URL_WORKSPACE_RECHECK_TIMEOUT_MS)
    // Picking another workspace mid-recheck is a decision; a late result must not bounce it back.
    const unsubscribe = useAppStore.subscribe((s) => {
      if (!settled && s.activeDomainId !== heldActiveDomainId) giveUp()
    })

    void revalidateDomains({ fresh: true }).then((result) => {
      // A skip means a full load owns the list: it drops the hold and re-runs this effect
      // when it lands. A failure has no answer to act on, so the timeout decides.
      if (result !== "fetched" || !mountedRef.current) return
      if (!settled) {
        giveUp()
        return
      }
      // The recheck timed out but the late answer has the workspace: return to the
      // link, unless the user has moved on from the fallback since. Only shortly after
      // the fallback, so a stalled answer can't yank someone out of a thread they're using.
      if (!fallback || Date.now() - fallback.at > URL_WORKSPACE_RECHECK_TIMEOUT_MS) return
      const s = useAppStore.getState()
      if (
        s.domains.some((d) => d.id === urlWorkspaceId) &&
        s.activeDomainId === fallback.activeDomainId &&
        s.threadId === fallback.threadId &&
        currentUrlRef.current.workspaceId === fallback.activeDomainId
      ) {
        navigate(chatUrl(urlWorkspaceId, fallback.linkThreadId, s.domains), { replace: true })
      }
    })
    return () => {
      settled = true
      clearTimeout(timer)
      unsubscribe()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [awaitingUrlWorkspace, urlWorkspaceId, revalidateDomains])

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

    // ChatRedirect can select the default workspace before this route mounts,
    // so opening it may not require a store change. The URL still represents a
    // real visit and should put the workspace in Recent.
    if (domains.some((d) => d.id === urlWorkspaceId)) {
      recordWorkspaceUse(urlWorkspaceId)
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
    if (!activeDomainId || awaitingUrlWorkspace) return
    // URL adoption above updates Zustand synchronously. Skip only the old
    // render, not the next render that carries the adopted workspace/thread.
    const current = useAppStore.getState()
    if (current.activeDomainId !== activeDomainId || current.threadId !== threadId) return
    if (
      syncedRef.current.workspaceId === activeDomainId &&
      syncedRef.current.threadId === (threadId || null)
    ) {
      return
    }

    const target = chatUrl(activeDomainId, threadId || null)

    syncedRef.current = { workspaceId: activeDomainId, threadId: threadId || null }
    // Filling a bare URL normalizes that entry; pushing would trap Back on
    // the bare entry, which would immediately forward to this thread again.
    navigate(target, { replace: urlWorkspaceId === activeDomainId && !urlThreadId })
    // A bare URL or newly loaded workspace can need reconciliation even when
    // the store identity did not change.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeDomainId, threadId, urlWorkspaceId, urlThreadId, domainsStatus, domains, awaitingUrlWorkspace])

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
    // Store → URL may already have queued a navigation in this effect pass.
    // Do not replace its thread-bearing target with the still-old URL params.
    if (
      syncedRef.current.workspaceId !== urlWorkspaceId ||
      syncedRef.current.threadId !== (urlThreadId ?? null)
    ) return

    const canonical = chatUrl(activeDomainId, urlThreadId ?? null)
    if (canonical !== location.pathname) {
      navigate(canonical, { replace: true })
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeDomainId, urlWorkspaceId, urlThreadId, domains, location.pathname])
}
