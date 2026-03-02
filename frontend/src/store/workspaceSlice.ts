import type { StateCreator } from "zustand"
import { api, ApiError, setActiveCustomWorkspaceId } from "@/api/client"

export interface CustomWorkspaceTenant {
  id: string
  tenant_workspace_id: string
  tenant_id: string
  tenant_name: string
  added_at: string
}

export interface WorkspaceMember {
  id: string
  user_id: string
  email: string
  role: "owner" | "editor" | "viewer"
  joined_at: string
}

export interface CustomWorkspace {
  id: string
  name: string
  description: string
  tenant_count: number
  member_count: number
  role: string
  created_at: string
  updated_at: string
}

export interface CustomWorkspaceDetail
  extends Omit<CustomWorkspace, "tenant_count" | "member_count" | "role"> {
  system_prompt: string
  tenants: CustomWorkspaceTenant[]
  members: WorkspaceMember[]
}

type WorkspaceMode = "tenant" | "custom"

export interface WorkspaceSlice {
  customWorkspaces: CustomWorkspace[]
  activeCustomWorkspaceId: string | null
  activeCustomWorkspace: CustomWorkspaceDetail | null
  workspaceMode: WorkspaceMode
  customWorkspacesStatus: "idle" | "loading" | "loaded" | "error"
  customWorkspacesError: string | null
  enterError: string | null
  missingTenants: string[]
  workspaceActions: {
    fetchCustomWorkspaces: () => Promise<void>
    enterCustomWorkspace: (id: string) => Promise<void>
    exitCustomWorkspace: () => void
    createCustomWorkspace: (data: {
      name: string
      description?: string
      tenant_workspace_ids?: string[]
      tenant_ids?: string[]
    }) => Promise<CustomWorkspaceDetail>
    addTenantToWorkspace: (
      workspaceId: string,
      tenantId: string,
    ) => Promise<CustomWorkspaceTenant>
    removeTenantFromWorkspace: (
      workspaceId: string,
      cwtId: string,
    ) => Promise<void>
  }
}

export const createWorkspaceSlice: StateCreator<WorkspaceSlice, [], [], WorkspaceSlice> = (
  set,
) => ({
  customWorkspaces: [],
  activeCustomWorkspaceId: null,
  activeCustomWorkspace: null,
  workspaceMode: "tenant",
  customWorkspacesStatus: "idle",
  customWorkspacesError: null,
  enterError: null,
  missingTenants: [],
  workspaceActions: {
    fetchCustomWorkspaces: async () => {
      set({ customWorkspacesStatus: "loading", customWorkspacesError: null })
      try {
        const workspaces = await api.get<CustomWorkspace[]>("/api/custom-workspaces/")
        set({
          customWorkspaces: workspaces,
          customWorkspacesStatus: "loaded",
          customWorkspacesError: null,
        })
      } catch (error) {
        set({
          customWorkspacesStatus: "error",
          customWorkspacesError:
            error instanceof Error ? error.message : "Failed to load custom workspaces",
        })
      }
    },

    enterCustomWorkspace: async (id: string) => {
      set({ enterError: null, missingTenants: [] })
      try {
        const detail = await api.post<CustomWorkspaceDetail>(
          `/api/custom-workspaces/${id}/enter/`,
        )
        setActiveCustomWorkspaceId(id)
        set({
          activeCustomWorkspaceId: id,
          activeCustomWorkspace: detail,
          workspaceMode: "custom",
          enterError: null,
          missingTenants: [],
        })
      } catch (error) {
        if (error instanceof ApiError && error.status === 403 && error.body) {
          const missing = Array.isArray(error.body.missing_tenants)
            ? (error.body.missing_tenants as string[])
            : []
          set({
            enterError: error.message,
            missingTenants: missing,
          })
        } else {
          set({
            enterError: error instanceof Error ? error.message : "Failed to enter workspace",
            missingTenants: [],
          })
        }
      }
    },

    exitCustomWorkspace: () => {
      setActiveCustomWorkspaceId(null)
      set({
        activeCustomWorkspaceId: null,
        activeCustomWorkspace: null,
        workspaceMode: "tenant",
        enterError: null,
        missingTenants: [],
      })
    },

    createCustomWorkspace: async (data) => {
      const detail = await api.post<CustomWorkspaceDetail>("/api/custom-workspaces/", data)
      set((state) => ({
        customWorkspaces: [
          ...state.customWorkspaces,
          {
            id: detail.id,
            name: detail.name,
            description: detail.description,
            tenant_count: detail.tenants.length,
            member_count: detail.members.length,
            role: "owner",
            created_at: detail.created_at,
            updated_at: detail.updated_at,
          },
        ],
      }))
      return detail
    },

    addTenantToWorkspace: async (workspaceId, tenantId) => {
      const cwt = await api.post<CustomWorkspaceTenant>(
        `/api/custom-workspaces/${workspaceId}/tenants/`,
        { tenant_id: tenantId },
      )
      set((state) => {
        const active = state.activeCustomWorkspace
        if (active && active.id === workspaceId) {
          return {
            activeCustomWorkspace: {
              ...active,
              tenants: [...active.tenants, cwt],
            },
          }
        }
        return {}
      })
      return cwt
    },

    removeTenantFromWorkspace: async (workspaceId, cwtId) => {
      await api.delete(`/api/custom-workspaces/${workspaceId}/tenants/${cwtId}/`)
      set((state) => {
        const active = state.activeCustomWorkspace
        if (active && active.id === workspaceId) {
          return {
            activeCustomWorkspace: {
              ...active,
              tenants: active.tenants.filter((t) => t.id !== cwtId),
            },
          }
        }
        return {}
      })
    },
  },
})
