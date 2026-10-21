import { providerShortLabel } from "@/components/WorkspaceBadge/providerMeta"

// "Loading" and "Materializing" read as jargon in user testing (#916); say what importing is.
export const IMPORT_EXPLAINER =
  "Scout copies each data source so it can answer questions. "
  + "This happens the first time a source is used and whenever its data is refreshed."

/** "Importing from CommCare Connect…" when every source is from one provider, else "Importing data…". */
export function importingLabel(
  providers: string | null | undefined | ReadonlyArray<string | null | undefined>,
): string {
  const list = typeof providers === "string" || providers == null ? [providers] : providers
  const labels = new Set(list.map((p) => (p ? providerShortLabel(p) : null)))
  const [only] = labels
  return labels.size === 1 && only ? `Importing from ${only}…` : "Importing data…"
}

/** "Importing visits from CommCare Connect…", falling back to whichever half is known. */
export function importingSourceLabel(
  source: string | null | undefined,
  provider: string | null | undefined,
): string {
  const short = provider ? providerShortLabel(provider) : null
  if (source && short) return `Importing ${source} from ${short}…`
  if (source) return `Importing ${source}…`
  return importingLabel(provider)
}
