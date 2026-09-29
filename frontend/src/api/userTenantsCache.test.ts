import { beforeEach, describe, expect, it, vi } from "vitest"

import { api } from "./client"
import { clearUserTenantsCache, getUserTenantsCached, refreshUserTenants } from "./userTenantsCache"

vi.mock("./client", () => ({ api: { get: vi.fn() } }))

beforeEach(() => {
  vi.clearAllMocks()
  clearUserTenantsCache()
  vi.mocked(api.get).mockResolvedValue([])
})

describe("userTenantsCache", () => {
  it("does not ask the server to bypass its refresh cache on a normal load", async () => {
    await getUserTenantsCached("user")

    expect(api.get).toHaveBeenCalledExactlyOnceWith("/api/auth/tenants/")
  })

  it("asks the server to re-check upstream access on an explicit refresh", async () => {
    await getUserTenantsCached("user")
    await refreshUserTenants("user")

    expect(api.get).toHaveBeenLastCalledWith("/api/auth/tenants/?refresh=1")
  })
})
