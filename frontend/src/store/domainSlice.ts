import type { StateCreator } from "zustand"
import { api } from "@/api/client"
import { workspaceApi, type WorkspaceListItem } from "@/api/workspaces"

// TenantMembership kept as alias so existing imports continue to work
export type TenantMembership = WorkspaceListItem & {
  // Legacy compat fields — kept so code referencing these doesn't break at compile time
  provider?: string
  tenant_id?: string
  tenant_name?: string
}

export type DomainsStatus = "idle" | "loading" | "loaded" | "error"

export interface DomainSlice {
  domains: TenantMembership[]
  activeDomainId: string | null
  domainsStatus: DomainsStatus
  domainsError: string | null
  domainActions: {
    fetchDomains: () => Promise<void>
    setActiveDomain: (id: string) => void
    setActiveDomainByTenantId: (provider: string, tenantId: string) => void
    ensureTenant: (provider: string, tenantId: string) => Promise<void>
  }
}

export const createDomainSlice: StateCreator<DomainSlice, [], [], DomainSlice> = (set, get) => ({
  domains: [],
  activeDomainId: null,
  domainsStatus: "idle",
  domainsError: null,
  domainActions: {
    fetchDomains: async () => {
      set({ domainsStatus: "loading", domainsError: null })
      try {
        const domains = await workspaceApi.list()
        const activeDomainId = get().activeDomainId
        set({
          domains,
          domainsStatus: "loaded",
          domainsError: null,
          activeDomainId: activeDomainId ?? (domains[0]?.id ?? null),
        })
      } catch (error) {
        set({
          domainsStatus: "error",
          domainsError: error instanceof Error ? error.message : "Failed to load domains",
        })
      }
    },

    setActiveDomain: (id: string) => {
      set({ activeDomainId: id })
    },

    // eslint-disable-next-line @typescript-eslint/no-unused-vars
    setActiveDomainByTenantId: (_provider: string, _tenantId: string) => {
      // No-op: workspace-based API doesn't need tenant selection
    },

    ensureTenant: async (provider: string, tenantId: string) => {
      try {
        const result = await api.post<{ workspace_id?: string }>("/api/auth/tenants/ensure/", {
          provider,
          tenant_id: tenantId,
        })
        await get().domainActions.fetchDomains()
        if (result.workspace_id) {
          set({ activeDomainId: result.workspace_id })
        }
      } catch (error) {
        console.error("[Scout] Failed to ensure tenant:", error)
      }
    },
  },
})
