import type { StateCreator } from "zustand"
import { api } from "@/api/client"
import { workspaceApi, workspaceHasAccess, type WorkspaceListItem } from "@/api/workspaces"

// TenantMembership kept as alias so existing imports continue to work
export type TenantMembership = WorkspaceListItem & {
  // Legacy compat fields, kept so referencing code still typechecks
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
        // Default to the first workspace the user can still access, never an
        // orphaned one whose upstream access was removed — landing there would
        // just show the lost-access modal. A deep link to an orphan still works
        // (the URL→store sync adopts it); this only governs the no-URL default.
        const defaultId =
          (domains.find(workspaceHasAccess) ?? domains[0])?.id ?? null
        set({
          domains,
          domainsStatus: "loaded",
          domainsError: null,
          activeDomainId: activeDomainId ?? defaultId,
        })
      } catch (error) {
        set({
          domainsStatus: "error",
          domainsError: error instanceof Error ? error.message : "Failed to load domains",
        })
      }
    },

    setActiveDomain: (id: string) => {
      // Switching workspaces must NOT carry the thread over: grafting the old
      // workspace's thread id onto the new URL produces a "Thread not found"
      // chat. Reset to a fresh id. Deep links (URL → store) overwrite this via
      // the sync hook's selectThread(urlThreadId) immediately after.
      if (id !== get().activeDomainId) {
        // `threadId` lives in the UI slice; cast so this cross-slice write
        // typechecks (the slices share one store).
        ;(set as (partial: { activeDomainId: string; threadId: string }) => void)({
          activeDomainId: id,
          threadId: crypto.randomUUID(),
        })
      } else {
        set({ activeDomainId: id })
      }
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
        // Set before fetchDomains so it's preserved as the active id
        if (result.workspace_id) {
          set({ activeDomainId: result.workspace_id })
        }
        await get().domainActions.fetchDomains()
      } catch (error) {
        // Surface an error state rather than leaving the user on an empty
        // data-sources page that reads as "no opportunities" (07#6).
        console.error("[Scout] Failed to ensure tenant:", error)
        set({
          domainsStatus: "error",
          domainsError:
            error instanceof Error ? error.message : "Failed to set up your workspace",
        })
      }
    },
  },
})
