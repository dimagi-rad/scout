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

/**
 * "skipped": something else owns the list, so this call didn't update it: the list isn't
 * loaded (an initial or retried full load is running), or a newer request superseded it.
 */
export type RevalidateResult = "fetched" | "failed" | "skipped"

export interface DomainSlice {
  domains: TenantMembership[]
  activeDomainId: string | null
  workspaceGeneration: number
  domainsStatus: DomainsStatus
  domainsError: string | null
  /** Workspaces a background refresh found that weren't listed before: someone added you. */
  addedDomainIds: string[]
  domainActions: {
    fetchDomains: () => Promise<void>
    /**
     * Background refresh: never shows loading or error, and keeps state when nothing changed.
     * `fresh` never joins a request that started before the call.
     */
    revalidateDomains: (options?: { fresh?: boolean }) => Promise<RevalidateResult>
    setActiveDomain: (id: string) => void
    setActiveDomainByTenantId: (provider: string, tenantId: string) => void
    ensureTenant: (provider: string, tenantId: string) => Promise<void>
    dismissAddedDomain: (id: string) => void
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
function nextActiveDomainId(
  prev: TenantMembership[],
  next: TenantMembership[],
  activeId: string | null,
): string | null {
  if (activeId === null) return defaultDomainId(next)
  const removed = prev.some((d) => d.id === activeId) && !next.some((d) => d.id === activeId)
  return removed ? defaultDomainId(next) : activeId
}

// Same array when absent, so subscribers selecting the list don't re-render.
function withoutId(ids: string[], id: string): string[] {
  return ids.includes(id) ? ids.filter((other) => other !== id) : ids
}

export const createDomainSlice: StateCreator<DomainSlice & AccountSessionScope, [], [], DomainSlice> = (set, get) => {
  // Bumped by every list request, so a background result never overwrites a newer foreground one.
  let listRequestSeq = 0
  let revalidation: Promise<RevalidateResult> | null = null

  return {
    domains: [],
    activeDomainId: null,
    workspaceGeneration: 0,
    domainsStatus: "idle",
    domainsError: null,
    addedDomainIds: [],
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

      revalidateDomains: async ({ fresh = false } = {}) => {
        // An initial or retried load shows its own state; don't race it.
        if (get().domainsStatus !== "loaded") return "skipped"
        if (revalidation) {
          const joined = await revalidation
          if (!fresh) return joined
          // That request may predate what the caller is waiting for (a grant, #355).
          return get().domainActions.revalidateDomains()
        }
        listRequestSeq += 1
        const seq = listRequestSeq
        revalidation = workspaceApi
          .list()
          .then((domains): RevalidateResult => {
            if (seq !== listRequestSeq) return "skipped"
            const current = get()
            // A new array re-runs every subscriber (#355); publish only real changes.
            if (JSON.stringify(domains) === JSON.stringify(current.domains)) return "fetched"
            const activeDomainId = nextActiveDomainId(
              current.domains,
              domains,
              current.activeDomainId,
            )
            const known = new Set(current.domains.map((d) => d.id))
            // The active one is already open, e.g. a deep link waiting on this very refresh,
            // and one without upstream access would only open the lost-access gate.
            const added = domains
              .filter((d) => !known.has(d.id) && d.id !== activeDomainId && workspaceHasAccess(d))
              .map((d) => d.id)
            const accessible = new Set(domains.filter(workspaceHasAccess).map((d) => d.id))
            const kept = current.addedDomainIds.filter(
              (id) => accessible.has(id) && !added.includes(id),
            )
            set({
              domains,
              activeDomainId,
              addedDomainIds:
                added.length === 0 && kept.length === current.addedDomainIds.length
                  ? current.addedDomainIds
                  : [...kept, ...added],
            })
            return "fetched"
          })
          // The list on screen is still usable, so a failed background refresh stays silent.
          .catch((): RevalidateResult => "failed")
          .finally(() => {
            revalidation = null
          })
        return revalidation
      },

      dismissAddedDomain: (id: string) => {
        set({ addedDomainIds: withoutId(get().addedDomainIds, id) })
      },

      setActiveDomain: (id: string) => {
        if (!get().accountSession.isCurrent()) return
        recordWorkspaceUse(id)
        set({ activeDomainId: id, addedDomainIds: withoutId(get().addedDomainIds, id) })
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
