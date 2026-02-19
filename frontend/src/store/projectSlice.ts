import type { StateCreator } from "zustand"
import { api } from "@/api/client"

export interface Project {
  id: string
  name: string
  slug: string
  description: string
  role: string
}

/**
 * Project details returned from the API.
 */
export interface ProjectDetail extends Project {
  database_connection: string
  database_connection_name: string
  db_schema: string
  allowed_tables: string[]
  excluded_tables: string[]
  system_prompt: string
  max_rows_per_query: number
  max_query_timeout_seconds: number
  llm_model: string
  is_active: boolean
}

/**
 * Data for creating or updating a project.
 */
export type ProjectFormData = Partial<ProjectDetail>

export interface ProjectMember {
  id: string
  email: string
  name: string
  role: string
  created_at: string
}

export type ProjectsStatus = "idle" | "loading" | "loaded" | "error"

export interface ProjectSlice {
  projects: Project[]
  activeProjectId: string | null
  projectsStatus: ProjectsStatus
  projectsError: string | null
  currentProject: ProjectDetail | null
  projectMembers: ProjectMember[]
  projectActions: {
    fetchProjects: () => Promise<void>
    setActiveProject: (id: string) => void
    fetchProject: (id: string) => Promise<ProjectDetail>
    createProject: (data: ProjectFormData) => Promise<ProjectDetail>
    updateProject: (id: string, data: ProjectFormData) => Promise<ProjectDetail>
    deleteProject: (id: string) => Promise<void>
    fetchMembers: (projectId: string) => Promise<void>
    addMember: (projectId: string, email: string, role: string) => Promise<void>
    removeMember: (projectId: string, userId: string) => Promise<void>
  }
}

export const createProjectSlice: StateCreator<ProjectSlice, [], [], ProjectSlice> = (set, get) => ({
  projects: [],
  activeProjectId: null,
  projectsStatus: "idle",
  projectsError: null,
  currentProject: null,
  projectMembers: [],
  projectActions: {
    fetchProjects: async () => {
      set({ projectsStatus: "loading", projectsError: null })
      try {
        const projects = await api.get<Project[]>("/api/projects/")
        const activeProjectId = get().activeProjectId
        set({
          projects,
          projectsStatus: "loaded",
          projectsError: null,
          // Auto-select first project if none selected
          activeProjectId: activeProjectId ?? (projects[0]?.id ?? null),
        })
      } catch (error) {
        set({
          projectsStatus: "error",
          projectsError: error instanceof Error ? error.message : "Failed to load projects",
        })
      }
    },

    setActiveProject: (id: string) => {
      set({ activeProjectId: id })
    },

    fetchProject: async (id: string) => {
      try {
        const project = await api.get<ProjectDetail>(`/api/projects/${id}/`)
        set({ currentProject: project })
        return project
      } catch (error) {
        set({ currentProject: null })
        throw error
      }
    },

    createProject: async (data: ProjectFormData) => {
      const project = await api.post<ProjectDetail>("/api/projects/", data)
      const projects = get().projects
      set({ projects: [...projects, project] })
      return project
    },

    updateProject: async (id: string, data: ProjectFormData) => {
      const project = await api.put<ProjectDetail>(`/api/projects/${id}/`, data)
      const projects = get().projects.map((p) => (p.id === id ? { ...p, ...project } : p))
      set({
        projects,
        currentProject: get().currentProject?.id === id ? project : get().currentProject,
      })
      return project
    },

    deleteProject: async (id: string) => {
      await api.delete<void>(`/api/projects/${id}/`)
      const projects = get().projects.filter((p) => p.id !== id)
      const activeProjectId = get().activeProjectId === id ? (projects[0]?.id ?? null) : get().activeProjectId
      set({
        projects,
        activeProjectId,
        currentProject: get().currentProject?.id === id ? null : get().currentProject,
      })
    },

    fetchMembers: async (projectId: string) => {
      try {
        const members = await api.get<ProjectMember[]>(`/api/projects/${projectId}/members/`)
        set({ projectMembers: members })
      } catch {
        set({ projectMembers: [] })
      }
    },

    addMember: async (projectId: string, email: string, role: string) => {
      const member = await api.post<ProjectMember>(`/api/projects/${projectId}/members/`, { email, role })
      const members = get().projectMembers
      set({ projectMembers: [...members, member] })
    },

    removeMember: async (projectId: string, userId: string) => {
      await api.delete<void>(`/api/projects/${projectId}/members/${userId}/`)
      const members = get().projectMembers.filter((m) => m.id !== userId)
      set({ projectMembers: members })
    },
  },
})
