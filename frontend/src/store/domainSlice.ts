import type { StateCreator } from "zustand"
import { api } from "@/api/client"

export interface TenantMembership {
  id: string
  provider: string
  tenant_id: string
  tenant_name: string
  last_selected_at: string | null
}

/** Display label for a tenant — Connect opps are prefixed with the opp ID. */
export function tenantDisplayName(d: TenantMembership): string {
  if (d.provider === "commcare_connect") {
    return `${d.tenant_id} – ${d.tenant_name}`
  }
  return d.tenant_name
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
      // Skip if already loading (prevents duplicate fetches from Sidebar + ConnectionsPage)
      if (get().domainsStatus === "loading") return
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

    setActiveDomainByTenantId: (provider: string, tenantId: string) => {
      const match = get().domains.find(
        (d) => d.provider === provider && d.tenant_id === tenantId
      )
      if (match) {
        get().domainActions.setActiveDomain(match.id)
      }
    },

    ensureTenant: async (provider: string, tenantId: string) => {
      try {
        await api.post("/api/auth/tenants/ensure/", {
          provider,
          tenant_id: tenantId,
        })
        await get().domainActions.fetchDomains()
        get().domainActions.setActiveDomainByTenantId(provider, tenantId)
      } catch (error) {
        console.error("[Scout] Failed to ensure tenant:", error)
      }
    },
  },
})
