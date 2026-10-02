import { act, renderHook } from "@testing-library/react"
import { afterEach, describe, expect, it } from "vitest"

import { ApiError } from "@/api/client"
import { useAppStore } from "@/store/store"
import {
  actionFailure,
  READ_ONLY_DENIAL,
  useWorkspaceRole,
  writeErrorMessage,
} from "./useWorkspaceRole"
import type { WorkspaceListItem } from "@/api/workspaces"

function workspace(id: string, role: WorkspaceListItem["role"]) {
  return { id, role } as WorkspaceListItem
}

afterEach(() => useAppStore.setState({ domains: [], activeDomainId: null }))

describe("useWorkspaceRole", () => {
  it("reads the active workspace role by default", () => {
    useAppStore.setState({
      domains: [workspace("ws-read", "read"), workspace("ws-manage", "manage")],
      activeDomainId: "ws-read",
    })
    const { result } = renderHook(() => useWorkspaceRole())
    expect(result.current).toEqual({ role: "read", canWrite: false })

    act(() => useAppStore.setState({ activeDomainId: "ws-manage" }))
    expect(result.current).toEqual({ role: "manage", canWrite: true })
  })

  it("reads an explicit workspace over the active one", () => {
    useAppStore.setState({
      domains: [workspace("ws-read", "read"), workspace("ws-rw", "read_write")],
      activeDomainId: "ws-read",
    })
    const { result } = renderHook(() => useWorkspaceRole("ws-rw"))
    expect(result.current).toEqual({ role: "read_write", canWrite: true })
  })

  it("stays writable while the role is unknown so the server remains the gate", () => {
    const { result } = renderHook(() => useWorkspaceRole("ws-missing"))
    expect(result.current).toEqual({ role: null, canWrite: true })
  })
})

describe("writeErrorMessage", () => {
  const generic = new ApiError(403, "Workspace not found or access denied.", {
    error: "Workspace not found or access denied.",
  })

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

  it("keeps lost-upstream-access copy even for read members", () => {
    const lost = new ApiError(403, "You no longer have access to: Alpha.", {
      error: "You no longer have access to: Alpha.",
      reason: "tenant_access_lost",
      lost_tenants: ["Alpha"],
    })
    expect(writeErrorMessage(lost, "Try again.", false)).toBe(lost.message)
  })

  it("keeps a specific server reason for read members", () => {
    const owner = new ApiError(403, "Only the thread owner can change sharing.", {
      error: "Only the thread owner can change sharing.",
    })
    expect(writeErrorMessage(owner, "Try again.", false)).toBe(owner.message)
  })

  it("keeps the fallback for a 403 without a JSON message such as a CSRF failure", () => {
    expect(writeErrorMessage(new ApiError(403, "Forbidden"), "Try again.", true)).toBe(
      "Try again.",
    )
  })

  it("keeps retry advice for a read member's CSRF failure", () => {
    expect(writeErrorMessage(new ApiError(403, "Forbidden"), "Try again.", false)).toBe(
      "Try again.",
    )
  })

  it("surfaces an unfinished verification's 503 copy", () => {
    const body = {
      error: "We couldn't verify your access to this workspace right now. Please retry shortly.",
      reason: "verification_unavailable",
      retryable: true,
    }
    expect(writeErrorMessage(new ApiError(503, body.error, body), "Try again.", false)).toBe(
      body.error,
    )
  })

  it("keeps the fallback for non-permission failures", () => {
    expect(writeErrorMessage(new ApiError(500, "boom"), "Try again.", false)).toBe("Try again.")
    expect(writeErrorMessage(new Error("offline"), "Try again.", false)).toBe("Try again.")
  })
})

describe("actionFailure", () => {
  it.each([
    ["a retryable structured 403", new ApiError(403, "Retry shortly.", { error: "Retry shortly.", retryable: true }), true],
    ["a final structured 403", new ApiError(403, "Reconnect.", { error: "Reconnect.", retryable: false }), false],
    ["a structured 403 without the flag", new ApiError(403, "Denied.", { error: "Denied." }), false],
    ["a non-JSON 403", new ApiError(403, "Forbidden", undefined), true],
    [
      "a verification 503",
      new ApiError(503, "Retry shortly.", {
        error: "Retry shortly.",
        reason: "verification_in_progress",
        retryable: true,
      }),
      true,
    ],
    ["a 500", new ApiError(500, "Failed to dispatch", { error: "Failed to dispatch" }), true],
    ["a network error", new TypeError("Failed to fetch"), true],
  ])("treats %s as retryable: %s", (_label, error, retryable) => {
    expect(actionFailure(error, "fallback", true).retryable).toBe(retryable)
  })
})
