import { act, renderHook } from "@testing-library/react"
import { afterEach, describe, expect, it } from "vitest"

import { ApiError } from "@/api/client"
import type { TenantMembership } from "@/store/domainSlice"
import { useAppStore } from "@/store/store"
import { READ_ONLY_DENIAL, useWorkspaceRole, writeErrorMessage } from "./useWorkspaceRole"

function workspace(id: string, role: TenantMembership["role"]) {
  return { id, role } as TenantMembership
}

afterEach(() => useAppStore.setState({ domains: [], activeDomainId: null }))

describe("useWorkspaceRole", () => {
  it("reads the active workspace role by default", () => {
    useAppStore.setState({
      domains: [workspace("ws-read", "read"), workspace("ws-manage", "manage")],
      activeDomainId: "ws-read",
    })
    const { result } = renderHook(() => useWorkspaceRole())
    expect(result.current).toEqual({ role: "read", canWrite: false, canManage: false })

    act(() => useAppStore.setState({ activeDomainId: "ws-manage" }))
    expect(result.current).toEqual({ role: "manage", canWrite: true, canManage: true })
  })

  it("reads an explicit workspace over the active one", () => {
    useAppStore.setState({
      domains: [workspace("ws-read", "read"), workspace("ws-rw", "read_write")],
      activeDomainId: "ws-read",
    })
    const { result } = renderHook(() => useWorkspaceRole("ws-rw"))
    expect(result.current).toEqual({ role: "read_write", canWrite: true, canManage: false })
  })

  it("stays writable while the role is unknown so the server remains the gate", () => {
    const { result } = renderHook(() => useWorkspaceRole("ws-missing"))
    expect(result.current).toEqual({ role: null, canWrite: true, canManage: false })
  })
})

describe("writeErrorMessage", () => {
  const generic = new ApiError(403, "Workspace not found or access denied.")

  it("explains the generic 403 as read-only when the user is a read member", () => {
    expect(writeErrorMessage(generic, "Try again.", false)).toBe(READ_ONLY_DENIAL)
  })

  it("recognises endpoints that name the role requirement", () => {
    const named = new ApiError(403, "Read-write or manage role required to annotate tables.")
    expect(writeErrorMessage(named, "Try again.", true)).toBe(READ_ONLY_DENIAL)
  })

  it("surfaces other 403 messages verbatim for writers", () => {
    expect(writeErrorMessage(generic, "Try again.", true)).toBe(generic.message)
  })

  it("keeps the fallback for non-permission failures", () => {
    expect(writeErrorMessage(new ApiError(500, "boom"), "Try again.", false)).toBe("Try again.")
    expect(writeErrorMessage(new Error("offline"), "Try again.", false)).toBe("Try again.")
  })
})
