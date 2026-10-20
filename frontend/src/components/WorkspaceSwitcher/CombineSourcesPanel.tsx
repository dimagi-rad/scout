import { type Dispatch, type SetStateAction, useEffect, useMemo, useRef, useState } from "react"
import { ArrowLeft, Loader2, Search, X } from "lucide-react"
import type { UserTenant } from "@/api/auth"
import { getUserTenantsCached } from "@/api/userTenantsCache"
import { getProviderMeta } from "@/components/WorkspaceBadge/providerMeta"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { ConnectSizeNotice } from "@/components/ConnectSizeNotice"
import { countConnectSources } from "@/lib/connectAccessCheck"
import { combinedWorkspaceName, searchSources } from "@/lib/sourceBasket"
import { useAppStore } from "@/store/store"

interface Props {
  /** Held by the switcher so stepping back to the list keeps the ticks. */
  basket: UserTenant[]
  setBasket: Dispatch<SetStateAction<UserTenant[]>>
  onBack: () => void
  onCombine: (name: string, sourceIds: string[]) => void
}

/**
 * Pick sources to build one multi-source workspace from (#916). Search-first: users
 * hold hundreds of sources, so nothing lists until they type, and ticks survive new
 * searches by living in the basket rather than in the result rows.
 */
export function CombineSourcesPanel({ basket, setBasket, onBack, onCombine }: Props) {
  const userId = useAppStore((s) => s.user?.id)
  const [sources, setSources] = useState<UserTenant[] | null>(null)
  const [loadError, setLoadError] = useState(false)
  const [query, setQuery] = useState("")
  const searchRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    if (!userId) return
    let cancelled = false
    getUserTenantsCached(userId).then(
      (data) => {
        if (!cancelled) setSources(data)
      },
      () => {
        if (!cancelled) setLoadError(true)
      },
    )
    return () => {
      cancelled = true
    }
  }, [userId])

  const { matches, hidden } = useMemo(
    () => searchSources(sources ?? [], query),
    [sources, query],
  )
  const ticked = useMemo(() => new Set(basket.map((s) => s.tenant_uuid)), [basket])
  const connectCount = countConnectSources(basket)

  function toggle(source: UserTenant) {
    setBasket((prev) =>
      prev.some((s) => s.tenant_uuid === source.tenant_uuid)
        ? prev.filter((s) => s.tenant_uuid !== source.tenant_uuid)
        : [...prev, source],
    )
  }

  function combine() {
    onCombine(
      combinedWorkspaceName(basket.map((s) => s.tenant_name)),
      basket.map((s) => s.tenant_uuid),
    )
  }

  return (
    <div className="flex min-h-0 flex-col" data-testid="workspace-combine-panel">
      <div className="flex items-center gap-1 border-b px-1 py-1">
        <button
          type="button"
          onClick={onBack}
          aria-label="Back to workspaces"
          data-testid="workspace-combine-back"
          className="flex h-7 w-7 items-center justify-center rounded-sm text-muted-foreground hover:bg-accent hover:text-foreground"
        >
          <ArrowLeft className="h-3.5 w-3.5" aria-hidden />
        </button>
        <p className="text-sm font-medium">Combine sources into a new workspace</p>
      </div>

      <div className="border-b p-2">
        <div className="relative">
          <Search className="absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
          <Input
            ref={searchRef}
            autoFocus
            placeholder="Search your data sources..."
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            className="h-8 pl-7 text-sm"
            data-testid="workspace-basket-search"
          />
        </div>
      </div>

      {basket.length > 0 && (
        <div
          className="flex max-h-24 flex-wrap items-center gap-1 overflow-y-auto border-b p-2"
          data-testid="workspace-basket"
        >
          {basket.map((s) => {
            const { Icon } = getProviderMeta(s.provider)
            return (
              <span
                key={s.tenant_uuid}
                data-testid={`workspace-basket-chip-${s.tenant_uuid}`}
                className="flex max-w-full items-center gap-1 rounded-full bg-accent py-0.5 pl-2 pr-1 text-xs"
              >
                <Icon className="h-3 w-3 shrink-0 text-muted-foreground" aria-hidden />
                <span className="truncate">{s.tenant_name}</span>
                <button
                  type="button"
                  onClick={() => {
                    toggle(s)
                    // The button leaves with its chip; keep keyboard users in the panel.
                    searchRef.current?.focus()
                  }}
                  aria-label={`Remove ${s.tenant_name}`}
                  className="rounded-full p-0.5 text-muted-foreground hover:text-foreground"
                >
                  <X className="h-3 w-3" aria-hidden />
                </button>
              </span>
            )
          })}
        </div>
      )}

      <div className="max-h-60 min-h-0 overflow-y-auto p-1" data-testid="workspace-basket-results">
        {loadError ? (
          <p className="px-2 py-4 text-center text-sm text-destructive">
            Couldn't load your data sources. Go back and try again.
          </p>
        ) : sources === null ? (
          <p className="flex items-center justify-center gap-2 px-2 py-4 text-sm text-muted-foreground">
            <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden />
            Loading your data sources…
          </p>
        ) : !query.trim() ? (
          <p className="px-2 py-4 text-center text-sm text-muted-foreground">
            Search {sources.length} data sources by name, then tick the ones to combine.
          </p>
        ) : matches.length === 0 ? (
          <p className="px-2 py-4 text-center text-sm text-muted-foreground">
            No data sources match.
          </p>
        ) : (
          <>
            {matches.map((s) => {
              const { Icon } = getProviderMeta(s.provider)
              return (
                <label
                  key={s.tenant_uuid}
                  className="flex cursor-pointer items-center gap-2 rounded-sm px-2 py-1.5 text-sm hover:bg-accent"
                >
                  <input
                    type="checkbox"
                    checked={ticked.has(s.tenant_uuid)}
                    onChange={() => toggle(s)}
                    data-testid={`workspace-basket-option-${s.tenant_uuid}`}
                  />
                  <Icon className="h-3.5 w-3.5 shrink-0 text-muted-foreground" aria-hidden />
                  <span className="truncate">{s.tenant_name}</span>
                </label>
              )
            })}
            {hidden > 0 && (
              <p
                className="px-2 py-1.5 text-xs text-muted-foreground"
                data-testid="workspace-basket-more"
              >
                {hidden} more — refine your search
              </p>
            )}
          </>
        )}
      </div>

      <ConnectSizeNotice
        count={connectCount}
        lead="These sources include"
        testId="workspace-basket-connect-warning"
        className="mx-2 mb-1 max-h-28 overflow-y-auto"
        canSplit
      />

      <div className="flex items-center justify-between gap-2 border-t p-2">
        <Button
          type="button"
          variant="ghost"
          size="sm"
          onClick={() => {
            setBasket([])
            searchRef.current?.focus()
          }}
          disabled={basket.length === 0}
          data-testid="workspace-basket-clear"
        >
          Clear
        </Button>
        <div className="flex items-center gap-2">
          {basket.length < 2 && (
            <span className="text-xs text-muted-foreground" data-testid="workspace-basket-hint">
              Tick 2 or more
            </span>
          )}
          <Button
            type="button"
            size="sm"
            onClick={combine}
            disabled={basket.length < 2}
            data-testid="workspace-basket-combine"
          >
            {basket.length < 2 ? "Combine" : `Combine ${basket.length}`}
          </Button>
        </div>
      </div>
    </div>
  )
}
