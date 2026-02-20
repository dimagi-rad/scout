import type { StateCreator } from "zustand"
import { api } from "@/api/client"

export interface TenantMembership {
  id: string
  provider: string
  tenant_id: string
  tenant_name: string
  last_selected_at: string | null
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
        const domains = await api.get<TenantMembership[]>("/api/auth/tenants/")
        const activeDomainId = get().activeDomainId
        set({
          domains,
          domainsStatus: "loaded",
          domainsError: null,
          activeDomainId: activeDomainId ?? (domains[0]?.id ?? null),
        })
        // Mark as selected on backend
        const selected = activeDomainId ?? domains[0]?.id
        if (selected) {
          api.post("/api/auth/tenants/select/", { tenant_id: selected }).catch(() => {})
        }
      } catch (error) {
        set({
          domainsStatus: "error",
          domainsError: error instanceof Error ? error.message : "Failed to load domains",
        })
      }
    },

    setActiveDomain: (id: string) => {
      set({ activeDomainId: id })
      api.post("/api/auth/tenants/select/", { tenant_id: id }).catch(() => {})
    },
  },
})
