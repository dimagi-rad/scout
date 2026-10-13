import type { UserTenant } from "@/api/auth"
import { getProviderMeta } from "@/components/WorkspaceBadge/providerMeta"
import { localIsoDate } from "@/lib/localDate"

import type { FacetDef } from "./facets"

export const CONNECT_PROVIDER = "commcare_connect"
export const UNKNOWN = "unknown"
// A colon cannot occur in an org slug or a program id, so this never collides with one.
export const NONE = ":none"

function connectAttributes(t: UserTenant): Record<string, unknown> | undefined {
  if (t.provider !== CONNECT_PROVIDER) return undefined
  const attrs = t.attributes
  return attrs !== null && typeof attrs === "object" && !Array.isArray(attrs) ? attrs : {}
}

function nonEmptyString(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value.trim() : undefined
}

// end_date is a calendar date; compare its day against the local day so an
// opportunity ending today still counts as active today.
function endDateHasPassed(value: unknown, today: string): boolean {
  const date = nonEmptyString(value)
  if (!date || !/^\d{4}-\d{2}-\d{2}/.test(date)) return false
  return date.slice(0, 10) < today
}

// getValue runs per row per facet on every keystroke; format the day once per minute.
// Keyed by the minute, so a clock moved backwards (or a faked one in tests) is honoured.
let cachedToday = { minute: Number.NaN, day: "" }
function currentDay(): string {
  const now = Date.now()
  const minute = Math.floor(now / 60_000)
  if (minute !== cachedToday.minute) cachedToday = { minute, day: localIsoDate(new Date(now)) }
  return cachedToday.day
}

export function connectStatus(t: UserTenant, today = currentDay()): string | undefined {
  const attrs = connectAttributes(t)
  if (!attrs) return undefined
  if (endDateHasPassed(attrs.end_date, today)) return "inactive"
  if (typeof attrs.is_active === "boolean") return attrs.is_active ? "active" : "inactive"
  return UNKNOWN
}

export function connectType(t: UserTenant): string | undefined {
  const attrs = connectAttributes(t)
  if (!attrs) return undefined
  if (typeof attrs.is_test === "boolean") return attrs.is_test ? "test" : "real"
  return UNKNOWN
}

function labelled(
  key: string,
  nameKey: string,
): Pick<FacetDef<UserTenant>, "getValue" | "optionLabel"> {
  return {
    getValue: (t) => {
      const attrs = connectAttributes(t)
      if (!attrs) return undefined
      return nonEmptyString(attrs[key]) ?? NONE
    },
    optionLabel: (value, t) => {
      if (value === NONE) return "None"
      return nonEmptyString(connectAttributes(t)?.[nameKey])
    },
  }
}

// Without the backend's attributes (an older API), every Connect row would read
// Unknown/None; hiding the group then is more honest than offering a no-op filter.
const FACETED_ATTRIBUTES = ["is_active", "end_date", "organization", "program"]

function hasConnectAttributes(items: readonly UserTenant[]): boolean {
  return items.some((t) => {
    const attrs = connectAttributes(t)
    return !!attrs && FACETED_ATTRIBUTES.some((key) => attrs[key] !== null && attrs[key] !== undefined)
  })
}

const STATUS_LABELS: Record<string, string> = {
  active: "Active",
  inactive: "Inactive",
  [UNKNOWN]: "Unknown",
}
const TYPE_LABELS: Record<string, string> = { real: "Real", test: "Test", [UNKNOWN]: "Unknown" }

export const TENANT_FACETS: readonly FacetDef<UserTenant>[] = [
  {
    key: "provider",
    label: "Provider",
    getValue: (t) => t.provider,
    optionLabel: (value) => getProviderMeta(value).label,
    isAvailable: (items) => new Set(items.map((t) => t.provider)).size > 1,
  },
  {
    key: "status",
    label: "Status",
    group: "Connect",
    getValue: (t) => connectStatus(t),
    optionLabel: (value) => STATUS_LABELS[value] ?? value,
    valueOrder: ["active", "inactive", UNKNOWN],
    isAvailable: hasConnectAttributes,
  },
  {
    key: "type",
    label: "Type",
    group: "Connect",
    getValue: connectType,
    optionLabel: (value) => TYPE_LABELS[value] ?? value,
    valueOrder: ["real", "test", UNKNOWN],
    // Only meaningful once the backend reports is_test; otherwise every row is "Unknown".
    isAvailable: (items) =>
      items.some((t) => typeof connectAttributes(t)?.is_test === "boolean"),
  },
  {
    key: "organization",
    label: "Organization",
    shortLabel: "Org",
    group: "Connect",
    searchable: true,
    trailingValues: [NONE],
    ...labelled("organization", "organization_name"),
    isAvailable: hasConnectAttributes,
  },
  {
    key: "program",
    label: "Program",
    group: "Connect",
    searchable: true,
    trailingValues: [NONE],
    ...labelled("program", "program_name"),
    isAvailable: hasConnectAttributes,
  },
]

/** The pickers' text search: name or external id, ignoring a leading "#". */
export function normalizeTenantSearch(query: string): string {
  // A trailing separator ("523,") is the start of a list, not part of an id.
  return query.trim().replace(/^#/, "").replace(/[\s,;]+$/, "").toLowerCase()
}

export interface TenantIdList {
  /** Canonical Connect opportunity ids, in the order pasted. */
  ids: readonly string[]
  idSet: ReadonlySet<string>
  /** Tokens in the list that are not ids, e.g. a typo like "67S". */
  unrecognised: readonly string[]
}

const ID_TOKEN = /^\d+$/

/**
 * A pasted list of opportunity ids ("523, 524, 675" or one per line, "#" optional),
 * or null when the query is ordinary search text. It takes two or more ids that
 * outnumber the other tokens, so a single number or a name like "KMC 2024 Kenya"
 * still searches as before, while a typo in a list is reported, not silently
 * turned into a name search.
 */
export function parseTenantIdList(query: string): TenantIdList | null {
  const tokens = query
    .split(/[\s,;]+/)
    .map((token) => token.replace(/^#/, ""))
    .filter(Boolean)
  const numeric = tokens.filter((token) => ID_TOKEN.test(token))
  if (numeric.length < 2 || numeric.length * 2 <= tokens.length) return null
  // Connect ids are integers, so "0523" means 523.
  const ids = [...new Set(numeric.map((token) => token.replace(/^0+(?=\d)/, "")))]
  const unrecognised = [...new Set(tokens.filter((token) => !ID_TOKEN.test(token)))]
  return { ids, idSet: new Set(ids), unrecognised }
}

/**
 * `idList` is the parse of `normalized`; callers filtering many rows pass it
 * pre-computed, since this runs per row on every keystroke.
 */
export function tenantMatchesSearch(
  t: UserTenant,
  normalized: string,
  idList: TenantIdList | null = parseTenantIdList(normalized),
): boolean {
  if (!normalized) return true
  // "Opportunity ID" is Connect's; another provider's all-digit id is not one.
  if (idList) return t.provider === CONNECT_PROVIDER && idList.idSet.has(t.tenant_id)
  return (
    t.tenant_name.toLowerCase().includes(normalized) ||
    t.tenant_id.toLowerCase().includes(normalized)
  )
}

/** "523, 524, 675" — or the first `max` and "and N more" for a long paste. */
export function formatIdSample(ids: readonly string[], max = 10): string {
  if (ids.length <= max) return ids.join(", ")
  return `${ids.slice(0, max).join(", ")} and ${ids.length - max} more`
}
