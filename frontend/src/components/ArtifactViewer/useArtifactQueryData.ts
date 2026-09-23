import { useCallback, useEffect, useRef, useState } from "react"

import { api } from "@/api/client"
import type { QueryDataResponse } from "./types"
import type { ArtifactDetail, ArtifactQueryContext, DateRange } from "@/components/ArtifactGraph/types"

const EMPTY_SOURCES: Record<string, DateRange> = {}

export function useArtifactDateSources(artifact: ArtifactDetail | null) {
  const key = artifact ? `${artifact.id}:${artifact.version}:${artifact.date_context?.as_of ?? ""}` : ""
  const [state, setState] = useState({ key, sources: EMPTY_SOURCES })
  const update = useCallback((sources: Record<string, DateRange>) => setState({ key, sources }), [key])
  return [state.key === key ? state.sources : EMPTY_SOURCES, update] as const
}

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
