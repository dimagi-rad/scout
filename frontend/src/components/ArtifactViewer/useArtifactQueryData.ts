import { useCallback, useEffect, useRef, useState } from "react"

import { api } from "@/api/client"
import type { QueryDataResponse } from "./types"
import type { ArtifactQueryContext } from "@/components/ArtifactGraph/types"

export function useArtifactQueryData(artifactId: string, workspaceId: string, runtime?: ArtifactQueryContext) {
  const [queryData, setQueryData] = useState<QueryDataResponse | null>(null)
  const [isLoading, setIsLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const generation = useRef(0)
  const runtimeKey = JSON.stringify(runtime)

  const refetch = useCallback(async () => {
    const request = ++generation.current
    setIsLoading(true)
    setError(null)
    try {
      const endpoint = `/api/workspaces/${workspaceId}/artifacts/${artifactId}/query-data/`
      const data = runtimeKey
        ? await api.post<QueryDataResponse>(endpoint, JSON.parse(runtimeKey))
        : await api.get<QueryDataResponse>(endpoint)
      if (generation.current === request) setQueryData(data)
    } catch (e) {
      if (generation.current === request) setError(e instanceof Error ? e.message : "Failed to load query data")
    } finally {
      if (generation.current === request) setIsLoading(false)
    }
  }, [artifactId, workspaceId, runtimeKey])

  useEffect(() => {
    generation.current += 1
    setQueryData(null)
    setError(null)
    setIsLoading(false)
    return () => { generation.current += 1 }
  }, [artifactId, workspaceId, runtimeKey])

  return { queryData, isLoading, error, refetch, setQueryData }
}
