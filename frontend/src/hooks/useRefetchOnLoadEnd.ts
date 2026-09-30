import { useEffect, useRef, useState } from "react"

/**
 * Fetch workspace data that a load changes, refetching when the load ends.
 * Null until it arrives or if the request fails: callers treat it as
 * informational. Mount the caller with `key={workspaceId}`: a load blocks the
 * refetch, which would otherwise leave the previous workspace's data up.
 * `fetcher` must be stable (a module-level API function).
 */
export function useRefetchOnLoadEnd<T>(
  fetcher: (workspaceId: string) => Promise<T>,
  workspaceId: string,
  loading = false,
): T | null {
  const [data, setData] = useState<T | null>(null)
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
    if (loading) return
    const request = ++latestRequestRef.current
    fetcher(workspaceId)
      .then((next) => {
        if (mountedRef.current && request === latestRequestRef.current) setData(next)
      })
      .catch(() => {})
  }, [fetcher, workspaceId, loading])

  return data
}
