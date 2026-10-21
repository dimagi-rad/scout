import type { WorkspaceListItem } from "@/api/workspaces"

const INLINE_SOURCE_LIMIT = 3

// Users read a workspace as a single data source (#916); name the sources it spans.
export function workspaceSourcesLabel(ws: WorkspaceListItem): string {
  const names = (ws.tenants ?? []).map((t) => t.tenant_name)
  if (names.length === 0) return "No data sources"
  return `${names.length === 1 ? "Data source" : `${names.length} data sources`}: ${names.join(", ")}`
}

export function workspaceSourcesShortLabel(ws: WorkspaceListItem): string {
  const names = (ws.tenants ?? []).map((t) => t.tenant_name)
  if (names.length === 0) return "no data sources"
  const shown = names.slice(0, INLINE_SOURCE_LIMIT).join(", ")
  const more = names.length - INLINE_SOURCE_LIMIT
  return more > 0 ? `${shown} +${more} more` : shown
}
