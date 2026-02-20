import type { StateCreator } from "zustand"
import { api } from "@/api/client"

export type ArtifactType = "react" | "html" | "markdown" | "plotly" | "svg"

export interface ArtifactSummary {
  id: string
  title: string
  description: string
  artifact_type: ArtifactType
  version: number
  has_live_queries: boolean
  created_at: string
  updated_at: string
}

type ArtifactsStatus = "idle" | "loading" | "loaded" | "error"

interface ArtifactListResponse {
  results: ArtifactSummary[]
}

export interface ArtifactSlice {
  artifacts: ArtifactSummary[]
  artifactsStatus: ArtifactsStatus
  artifactsError: string | null
  artifactSearch: string
  artifactActions: {
    fetchArtifacts: (options?: { search?: string }) => Promise<void>
    updateArtifact: (artifactId: string, data: { title?: string; description?: string }) => Promise<void>
    deleteArtifact: (artifactId: string) => Promise<void>
    setArtifactSearch: (search: string) => void
  }
}

export const createArtifactSlice: StateCreator<ArtifactSlice, [], [], ArtifactSlice> = (set) => ({
  artifacts: [],
  artifactsStatus: "idle",
  artifactsError: null,
  artifactSearch: "",
  artifactActions: {
    fetchArtifacts: async (options) => {
      set({ artifactsStatus: "loading", artifactsError: null })
      try {
        const params = new URLSearchParams()
        if (options?.search) params.set("search", options.search)
        const qs = params.toString()
        const url = `/api/artifacts/${qs ? `?${qs}` : ""}`
        const response = await api.get<ArtifactListResponse>(url)
        set({
          artifacts: response.results,
          artifactsStatus: "loaded",
          artifactsError: null,
        })
      } catch (error) {
        set({
          artifactsStatus: "error",
          artifactsError: error instanceof Error ? error.message : "Failed to load artifacts",
        })
      }
    },
    updateArtifact: async (artifactId, data) => {
      const updated = await api.patch<{ id: string; title: string; description: string }>(
        `/api/artifacts/${artifactId}/`,
        data,
      )
      set((state) => ({
        artifacts: state.artifacts.map((a) =>
          a.id === artifactId ? { ...a, title: updated.title, description: updated.description } : a
        ),
      }))
    },
    deleteArtifact: async (artifactId) => {
      await api.delete(`/api/artifacts/${artifactId}/`)
      set((state) => ({
        artifacts: state.artifacts.filter((a) => a.id !== artifactId),
      }))
    },
    setArtifactSearch: (search) => set({ artifactSearch: search }),
  },
})
