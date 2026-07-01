import { useCallback, useEffect, useState } from "react"

import { api } from "@/api/client"
import type { QueryDataResponse } from "./types"

export function useArtifactQueryData(artifactId: string, workspaceId: string) {
  const [queryData, setQueryData] = useState<QueryDataResponse | null>(null)
  const [isLoading, setIsLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const refetch = useCallback(async () => {
    setIsLoading(true)
    setError(null)
    try {
      const data = await api.get<QueryDataResponse>(`/api/workspaces/${workspaceId}/artifacts/${artifactId}/query-data/`)
      setQueryData(data)
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load query data")
    } finally {
      setIsLoading(false)
    }
  }, [artifactId, workspaceId])

  useEffect(() => {
    setQueryData(null)
    setError(null)
  }, [artifactId, workspaceId])

  return { queryData, isLoading, error, refetch, setQueryData }
}
