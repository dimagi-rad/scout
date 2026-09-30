import { useEffect, useRef, useState } from "react"

/**
 * Fetch workspace data that a load changes, refetching when the load ends.
 * Null until it arrives, if the request fails (callers treat it as
 * informational), or while no workspace is selected. Data fetched for another
 * workspace is never returned, since a load can hold off the refetch after a switch.
 * `fetcher` must be stable (a module-level API function).
 */
export function useRefetchOnLoadEnd<T>(
  fetcher: (workspaceId: string) => Promise<T>,
  workspaceId: string | null,
  loading = false,
): T | null {
  const [fetched, setFetched] = useState<{ workspaceId: string; data: T } | null>(null)
  const mountedRef = useRef(true)
  const latestRequestRef = useRef(0)

  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
    }
  }, [])

  // A load starting mid-fetch must not discard the response already on its way
  // (so no per-effect cancel), but an older response must not overwrite a newer one.
  useEffect(() => {
    if (loading || !workspaceId) return
    const request = ++latestRequestRef.current
    fetcher(workspaceId)
      .then((data) => {
        if (mountedRef.current && request === latestRequestRef.current) {
          setFetched({ workspaceId, data })
        }
      })
      .catch(() => {})
  }, [fetcher, workspaceId, loading])

  return fetched && fetched.workspaceId === workspaceId ? fetched.data : null
}
