import { describe, expect, it } from "vitest"

import type { UserTenant } from "@/api/auth"

import { applyFacets, availableFacets, computeFacetOptions } from "./facets"
import {
  NONE,
  TENANT_FACETS,
  UNKNOWN,
  connectStatus,
  connectType,
  sourceFiltersStorageKey,
  tenantMatchesSearch,
  normalizeTenantSearch,
} from "./tenantFacets"

function tenant(
  id: string,
  provider: string,
  attributes?: unknown,
  name = id,
): UserTenant {
  return {
    id: `m-${id}`,
    provider,
    tenant_id: id,
    tenant_uuid: `uuid-${id}`,
    tenant_name: name,
    last_selected_at: null,
    attributes: attributes as Record<string, unknown> | undefined,
  }
}

const TODAY = "2026-10-09"

describe("connectStatus", () => {
  it("reads is_active", () => {
    expect(connectStatus(tenant("1", "commcare_connect", { is_active: true }), TODAY)).toBe(
      "active",
    )
    expect(connectStatus(tenant("1", "commcare_connect", { is_active: false }), TODAY)).toBe(
      "inactive",
    )
  })

  it("treats a past end date as inactive even when flagged active", () => {
    const t = tenant("1", "commcare_connect", { is_active: true, end_date: "2026-10-08" })
    expect(connectStatus(t, TODAY)).toBe("inactive")
    const endsToday = tenant("1", "commcare_connect", { is_active: true, end_date: TODAY })
    expect(connectStatus(endsToday, TODAY)).toBe("active")
  })

  it.each([
    ["missing attributes", undefined],
    ["null attributes", null],
    ["array attributes", []],
    ["missing is_active", {}],
    ["string is_active", { is_active: "true" }],
    ["garbage end_date", { end_date: "soon" }],
  ])("is Unknown with %s", (_label, attrs) => {
    expect(connectStatus(tenant("1", "commcare_connect", attrs), TODAY)).toBe(UNKNOWN)
  })

  it("does not apply to other providers", () => {
    expect(connectStatus(tenant("1", "commcare", { is_active: false }), TODAY)).toBeUndefined()
  })
})

describe("connectType", () => {
  it("maps is_test and falls back to Unknown", () => {
    expect(connectType(tenant("1", "commcare_connect", { is_test: true }))).toBe("test")
    expect(connectType(tenant("1", "commcare_connect", { is_test: false }))).toBe("real")
    expect(connectType(tenant("1", "commcare_connect", { is_test: 1 }))).toBe(UNKNOWN)
    expect(connectType(tenant("1", "ocs", { is_test: true }))).toBeUndefined()
  })
})

const sources = [
  tenant("cc", "commcare", {}),
  tenant("ocs", "ocs"),
  tenant("c1", "commcare_connect", {
    is_active: true,
    is_test: false,
    organization: "dimagi",
    organization_name: "Dimagi",
    program: "p1",
    program_name: "Nutrition",
  }),
  tenant("c2", "commcare_connect", { is_active: false, is_test: true, organization: "dimagi" }),
  tenant("c3", "commcare_connect", { is_active: true }),
]

describe("TENANT_FACETS", () => {
  it("never hides CommCare or OCS rows with a Connect facet", () => {
    const kept = applyFacets(sources, TENANT_FACETS, { status: ["active"], type: ["real"] })
    expect(kept.map((t) => t.tenant_id)).toEqual(["cc", "ocs", "c1"])
  })

  it("narrows to Connect via the provider facet", () => {
    const kept = applyFacets(sources, TENANT_FACETS, {
      provider: ["commcare_connect"],
      status: ["inactive"],
    })
    expect(kept.map((t) => t.tenant_id)).toEqual(["c2"])
  })

  it("labels organizations by name, with None for rows lacking one", () => {
    const options = computeFacetOptions(sources, TENANT_FACETS, {})
    expect(options.organization).toEqual([
      { value: "dimagi", label: "Dimagi", count: 2 },
      { value: NONE, label: "None", count: 1 },
    ])
    expect(options.program.map((o) => o.label)).toEqual(["Nutrition", "None"])
    expect(options.provider.map((o) => o.label)).toEqual([
      "CommCare",
      "CommCare Connect",
      "Open Chat Studio",
    ])
    expect(options.type.map((o) => [o.value, o.count])).toEqual([
      ["real", 1],
      ["test", 1],
      [UNKNOWN, 1],
    ])
  })

  it("hides Type when no row reports is_test", () => {
    const noTest = sources.map((t) => ({
      ...t,
      attributes: t.attributes ? { ...t.attributes, is_test: undefined } : t.attributes,
    }))
    expect(availableFacets(TENANT_FACETS, noTest).map((f) => f.key)).toEqual([
      "provider",
      "status",
      "organization",
      "program",
    ])
  })

  it("hides the Connect group when the API sends no attributes, and Provider for one provider", () => {
    const legacy = [tenant("c1", "commcare_connect"), tenant("c2", "commcare_connect")]
    expect(availableFacets(TENANT_FACETS, legacy)).toEqual([])
  })
})

it("searches name or external id, ignoring a leading #", () => {
  const t = tenant("814", "commcare_connect", {}, "Kenya Nutrition")
  expect(tenantMatchesSearch(t, normalizeTenantSearch("#81"))).toBe(true)
  expect(tenantMatchesSearch(t, normalizeTenantSearch(" KENYA "))).toBe(true)
  expect(tenantMatchesSearch(t, normalizeTenantSearch("ghana"))).toBe(false)
})

it("scopes the storage key to the user", () => {
  expect(sourceFiltersStorageKey("u1")).toBe("scout:source-filters:v1:u1")
  expect(sourceFiltersStorageKey(undefined)).toBeNull()
})
