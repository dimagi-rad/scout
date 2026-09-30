import { useEffect, useRef, useState } from "react"
import { workspaceApi, type WorkspaceDetail } from "@/api/workspaces"

/**
 * The workspace detail payload, refetched when a load ends since the data just
 * changed. Null until it arrives or if the request fails: callers treat it as
 * informational. Mount the caller with `key={workspaceId}`: a load blocks the
 * refetch, which would otherwise leave the previous workspace's detail up.
 */
export function useWorkspaceDetail(workspaceId: string, loading = false): WorkspaceDetail | null {
  const [detail, setDetail] = useState<WorkspaceDetail | null>(null)
  const mountedRef = useRef(true)

  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
    }
  }, [])

  // A load starting mid-fetch must not discard the response already on its way.
  useEffect(() => {
    if (loading) return
    workspaceApi
      .getDetail(workspaceId)
      .then((next) => {
        if (mountedRef.current) setDetail(next)
      })
      .catch(() => {})
  }, [workspaceId, loading])

  return detail
}
