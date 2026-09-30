import { describe, expect, it } from "vitest"

import type { SourceFreshnessDetail, WorkspaceFreshness } from "@/api/workspaces"
import { formatDataAge, staleData } from "./staleData"
import { freshness, freshSource } from "./testFixtures"

const NOW = Date.parse("2026-09-30T12:00:00Z")
const HOUR = 3600_000

function source(id: string, hoursAgo: number | null, extra: Partial<SourceFreshnessDetail> = {}) {
  return freshSource(`Source ${id}`, hoursAgo, extra, NOW)
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
    expect(staleData(freshness([source("a", 23)]), { now: NOW })).toBeNull()
  })

  it("reports the age once the oldest serving source passes the threshold", () => {
    expect(staleData(freshness([source("a", 72)]), { now: NOW })).toEqual({
      ageLabel: "3 days ago",
      oldestSourceName: null,
      reconnectProviders: [],
      refreshable: true,
    })
  })

  it("uses the server's threshold", () => {
    const f = freshness([source("a", 7)], { stale_data_banner_hours: 6 })
    expect(staleData(f, { now: NOW })?.ageLabel).toBe("7 hours ago")
  })

  it("defaults to 24 hours when the payload has no threshold", () => {
    const f = { ...freshness([source("a", 25)]) } as Partial<WorkspaceFreshness>
    delete f.stale_data_banner_hours
    expect(staleData(f as WorkspaceFreshness, { now: NOW })?.ageLabel).toBe("25 hours ago")
    expect(staleData(freshness([source("a", 23)]), { now: NOW })).toBeNull()
  })

  it("treats a non-positive threshold as off", () => {
    expect(
      staleData(freshness([source("a", 72)], { stale_data_banner_hours: 0 }), { now: NOW }),
    ).toBeNull()
  })

  it("judges by the oldest serving source and names it", () => {
    const f = freshness([source("a", 2), source("b", 30)])
    expect(staleData(f, { now: NOW })).toMatchObject({
      ageLabel: "30 hours ago",
      oldestSourceName: "Source b",
    })
  })

  it("ignores sources that are not serving", () => {
    const f = freshness([source("a", 2), source("b", 100, { serving: false })])
    expect(staleData(f, { now: NOW })).toBeNull()
  })

  it("is null while a load runs", () => {
    expect(staleData(freshness([source("a", 72)]), { now: NOW, loading: true })).toBeNull()
    expect(
      staleData(freshness([source("a", 72)], { in_progress: true }), { now: NOW }),
    ).toBeNull()
  })

  it("is null when nothing is loaded yet", () => {
    expect(staleData(freshness([source("a", null)]), { now: NOW })).toBeNull()
    expect(staleData(freshness([]), { now: NOW })).toBeNull()
  })

  it("is null when freshness could not be fetched", () => {
    expect(staleData(null, { now: NOW })).toBeNull()
  })

  it("names each provider whose expired sign-in needs a reconnect", () => {
    const f = freshness([
      source("a", 72, { reconnect: true }),
      source("b", 72, { reconnect: true }),
      source("c", 1, { provider: "ocs", provider_label: "Open Chat Studio" }),
    ])
    expect(staleData(f, { now: NOW })?.reconnectProviders).toEqual(["CommCare HQ"])
  })

  it("keeps Refresh when only a non-serving source needs a reconnect", () => {
    const f = freshness([
      source("a", 72),
      source("b", null, { reconnect: true, provider: "ocs", provider_label: "Open Chat Studio" }),
    ])
    expect(staleData(f, { now: NOW })?.reconnectProviders).toEqual([])
  })

  it("asks for a reconnect only for sources past the threshold", () => {
    const f = freshness([source("a", 100), source("b", 2, { reconnect: true })])
    expect(staleData(f, { now: NOW })).toMatchObject({
      reconnectProviders: [],
      refreshable: true,
    })
  })

  it("is refreshable while any stale source is still connected", () => {
    const f = freshness([source("a", 100), source("b", 72, { reconnect: true })])
    expect(staleData(f, { now: NOW })).toMatchObject({
      reconnectProviders: ["CommCare HQ"],
      refreshable: true,
    })
    const g = freshness([source("a", 100, { reconnect: true })])
    expect(staleData(g, { now: NOW })?.refreshable).toBe(false)
  })
})
