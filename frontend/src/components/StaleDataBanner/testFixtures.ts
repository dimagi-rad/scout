import type { SourceFreshnessDetail, WorkspaceFreshness } from "@/api/workspaces"

const HOUR = 3600_000

export function freshSource(
  name: string,
  hoursAgo: number | null,
  extra: Partial<SourceFreshnessDetail> = {},
  now = Date.now(),
): SourceFreshnessDetail {
  return {
    tenant_id: name,
    name,
    provider: "commcare",
    provider_label: "CommCare HQ",
    serving: hoursAgo !== null,
    last_fetched_at: hoursAgo === null ? null : new Date(now - hoursAgo * HOUR).toISOString(),
    reconnect: false,
    ...extra,
  }
}

export function freshness(
  sources: SourceFreshnessDetail[],
  extra: Partial<WorkspaceFreshness> = {},
): WorkspaceFreshness {
  return { stale_data_banner_hours: 24, in_progress: false, sources, ...extra }
}
