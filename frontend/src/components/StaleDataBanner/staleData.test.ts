import { describe, expect, it } from "vitest"

import type { WorkspaceDetail, WorkspaceSourceFreshness } from "@/api/workspaces"
import { formatDataAge, staleData } from "./staleData"

const NOW = Date.parse("2026-09-30T12:00:00Z")
const HOUR = 3600_000

function source(
  id: string,
  hoursAgo: number | null,
  extra: Partial<WorkspaceSourceFreshness> = {},
): WorkspaceSourceFreshness {
  return {
    tenant_id: id,
    tenant_name: `Source ${id}`,
    provider: "commcare",
    provider_label: "CommCare HQ",
    last_synced_at: hoursAgo === null ? null : new Date(NOW - hoursAgo * HOUR).toISOString(),
    serving: hoursAgo !== null,
    ...extra,
  }
}

function detail(sources: WorkspaceSourceFreshness[], extra: Partial<WorkspaceDetail> = {}) {
  return { id: "ws-1", sources, stale_data_banner_hours: 24, ...extra } as WorkspaceDetail
}

describe("formatDataAge", () => {
  it.each([
    [0.5, "0 hours ago"],
    [1, "1 hour ago"],
    [30, "30 hours ago"],
    [47.9, "47 hours ago"],
    [48, "2 days ago"],
    [72, "3 days ago"],
  ])("%s hours reads %s", (hours, label) => {
    expect(formatDataAge(hours * HOUR)).toBe(label)
  })
})

describe("staleData", () => {
  it("is null while the oldest serving source is under the threshold", () => {
    expect(staleData(detail([source("a", 23)]), { now: NOW })).toBeNull()
  })

  it("reports the age once the oldest serving source passes the threshold", () => {
    expect(staleData(detail([source("a", 72)]), { now: NOW })).toEqual({
      ageLabel: "3 days ago",
      oldestSourceName: null,
      reconnectProviders: [],
    })
  })

  it("uses the server's threshold", () => {
    const d = detail([source("a", 7)], { stale_data_banner_hours: 6 })
    expect(staleData(d, { now: NOW })?.ageLabel).toBe("7 hours ago")
  })

  it("defaults to 24 hours when the payload has no threshold", () => {
    const d = detail([source("a", 25)], { stale_data_banner_hours: undefined })
    expect(staleData(d, { now: NOW })?.ageLabel).toBe("25 hours ago")
  })

  it("judges by the oldest serving source and names it", () => {
    const d = detail([source("a", 2), source("b", 30)])
    expect(staleData(d, { now: NOW })).toMatchObject({
      ageLabel: "30 hours ago",
      oldestSourceName: "Source b",
    })
  })

  it("ignores sources that are not serving", () => {
    const d = detail([source("a", 2), source("b", 100, { serving: false })])
    expect(staleData(d, { now: NOW })).toBeNull()
  })

  it("is null while a load runs", () => {
    expect(staleData(detail([source("a", 72)]), { now: NOW, loading: true })).toBeNull()
    expect(staleData(detail([source("a", 72)], { in_progress: true }), { now: NOW })).toBeNull()
  })

  it("is null when nothing is loaded yet", () => {
    expect(staleData(detail([source("a", null)]), { now: NOW })).toBeNull()
    expect(staleData(detail([]), { now: NOW })).toBeNull()
  })

  it("is null when the member lost access to a source", () => {
    const d = detail([source("a", 72)], {
      missing_tenants: [{ tenant_id: "a" } as NonNullable<WorkspaceDetail["missing_tenants"]>[0]],
    })
    expect(staleData(d, { now: NOW })).toBeNull()
  })

  it("names each provider whose expired sign-in needs a reconnect", () => {
    const d = detail([
      source("a", 72, { reconnect: true }),
      source("b", 72, { reconnect: true }),
      source("c", 1, { provider: "ocs", provider_label: "Open Chat Studio" }),
    ])
    expect(staleData(d, { now: NOW })?.reconnectProviders).toEqual(["CommCare HQ"])
  })
})
