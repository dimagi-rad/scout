import type { StateCreator } from "zustand"
import { api } from "@/api/client"
import type { DomainSlice } from "./domainSlice"

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

export const createArtifactSlice: StateCreator<ArtifactSlice & DomainSlice, [], [], ArtifactSlice> = (set, get) => ({
  artifacts: [],
  artifactsStatus: "idle",
  artifactsError: null,
  artifactSearch: "",
  artifactActions: {
    fetchArtifacts: async (options) => {
      set({ artifactsStatus: "loading", artifactsError: null })
      try {
        const activeDomainId = get().activeDomainId
        if (!activeDomainId) throw new Error("No active domain selected.")
        const params = new URLSearchParams()
        if (options?.search) params.set("search", options.search)
        const qs = params.toString()
        const url = `/api/workspaces/${activeDomainId}/artifacts/${qs ? `?${qs}` : ""}`
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
      const activeDomainId = get().activeDomainId
      if (!activeDomainId) throw new Error("No active domain selected.")
      const updated = await api.patch<{ id: string; title: string; description: string }>(
        `/api/workspaces/${activeDomainId}/artifacts/${artifactId}/`,
        data,
      )
      set((state) => ({
        artifacts: state.artifacts.map((a) =>
          a.id === artifactId ? { ...a, title: updated.title, description: updated.description } : a
        ),
      }))
    },
    deleteArtifact: async (artifactId) => {
      const activeDomainId = get().activeDomainId
      if (!activeDomainId) throw new Error("No active domain selected.")
      await api.delete(`/api/workspaces/${activeDomainId}/artifacts/${artifactId}/`)
      set((state) => ({
        artifacts: state.artifacts.filter((a) => a.id !== artifactId),
      }))
    },
    setArtifactSearch: (search) => set({ artifactSearch: search }),
  },
})
