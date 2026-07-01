import { useCallback, useEffect, useState } from "react"

import { api } from "@/api/client"
import type { ArtifactDetail } from "@/components/ArtifactGraph"

export function useArtifactDetail(artifactId: string, workspaceId: string) {
  const [artifact, setArtifact] = useState<ArtifactDetail | null>(null)
  const [isLoading, setIsLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const refetch = useCallback(async () => {
    setIsLoading(true)
    setError(null)
    try {
      const data = await api.get<ArtifactDetail>(`/api/workspaces/${workspaceId}/artifacts/${artifactId}/data/`)
      setArtifact(data)
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load artifact")
    } finally {
      setIsLoading(false)
    }
  }, [artifactId, workspaceId])

  useEffect(() => {
    setArtifact(null)
    setError(null)
    void refetch()
  }, [refetch])

  return { artifact, isLoading, error, refetch }
}
