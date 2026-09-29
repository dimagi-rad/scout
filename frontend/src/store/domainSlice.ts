import type { StateCreator } from "zustand"
import { api } from "@/api/client"
import { workspaceApi, workspaceHasAccess, type WorkspaceListItem } from "@/api/workspaces"
import { recordWorkspaceUse } from "@/lib/recentWorkspaces"
import type { AccountSessionScope } from "./accountSession"

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
  workspaceGeneration: number
  domainsStatus: DomainsStatus
  domainsError: string | null
  domainActions: {
    fetchDomains: () => Promise<void>
    /** Background refresh: never shows loading or error, and keeps state when nothing changed. */
    revalidateDomains: () => Promise<void>
    setActiveDomain: (id: string) => void
    setActiveDomainByTenantId: (provider: string, tenantId: string) => void
    ensureTenant: (provider: string, tenantId: string) => Promise<void>
  }
}

// Default to the first workspace the user can still access, never an orphaned
// one whose upstream access was removed — landing there would just show the
// lost-access modal. A deep link to an orphan still works (the URL→store sync
// adopts it); this only governs the no-URL default.
function defaultDomainId(domains: TenantMembership[]): string | null {
  return (domains.find(workspaceHasAccess) ?? domains[0])?.id ?? null
}

// Dropped from the list (deleted, or you were removed): every lookup of it would
// now miss, and a missing role reads as writable, so move to the default. An id
// that was never listed, such as a deep link still being checked, is kept.
export function nextActiveDomainId(
  prev: TenantMembership[],
  next: TenantMembership[],
  activeId: string | null,
): string | null {
  if (activeId === null) return defaultDomainId(next)
  const removed = prev.some((d) => d.id === activeId) && !next.some((d) => d.id === activeId)
  return removed ? defaultDomainId(next) : activeId
}

export const createDomainSlice: StateCreator<DomainSlice & AccountSessionScope, [], [], DomainSlice> = (set, get) => {
  // Bumped by every list request, so a background result never overwrites a newer foreground one.
  let listRequestSeq = 0
  let revalidation: Promise<void> | null = null

  return {
    domains: [],
    activeDomainId: null,
    workspaceGeneration: 0,
    domainsStatus: "idle",
    domainsError: null,
    domainActions: {
      fetchDomains: async () => {
        listRequestSeq += 1
        set({ domainsStatus: "loading", domainsError: null })
        try {
          const domains = await workspaceApi.list()
          const current = get()
          set({
            domains,
            domainsStatus: "loaded",
            domainsError: null,
            activeDomainId: nextActiveDomainId(current.domains, domains, current.activeDomainId),
          })
        } catch (error) {
          set({
            domainsStatus: "error",
            domainsError: error instanceof Error ? error.message : "Failed to load domains",
          })
        }
      },

      revalidateDomains: () => {
        // An initial or retried load shows its own state; don't race it.
        if (get().domainsStatus !== "loaded") return Promise.resolve()
        if (revalidation) return revalidation
        listRequestSeq += 1
        const seq = listRequestSeq
        revalidation = workspaceApi
          .list()
          .then((domains) => {
            if (seq !== listRequestSeq) return
            const current = get()
            // A new array re-runs every subscriber (#355); publish only real changes.
            if (JSON.stringify(domains) === JSON.stringify(current.domains)) return
            set({
              domains,
              activeDomainId: nextActiveDomainId(current.domains, domains, current.activeDomainId),
            })
          })
          .catch(() => {
            // The list on screen is still usable, so a failed background refresh stays silent.
          })
          .finally(() => {
            revalidation = null
          })
        return revalidation
      },

      setActiveDomain: (id: string) => {
        if (!get().accountSession.isCurrent()) return
        recordWorkspaceUse(id)
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
  }
}
