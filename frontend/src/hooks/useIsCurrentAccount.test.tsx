import { StrictMode } from "react"
import { act, cleanup, renderHook } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it } from "vitest"
import { useAppStore } from "@/store/store"
import { useIsCurrentAccount } from "./useIsCurrentAccount"

const USER_A = {
  id: "user-a", email: "a@example.invalid", name: "A", is_staff: false, onboarding_complete: true,
}

beforeEach(() => useAppStore.setState({ user: USER_A }))
afterEach(() => {
  cleanup()
  useAppStore.setState({ user: null })
})

describe("useIsCurrentAccount", () => {
  it("stays current through StrictMode setup and same-account profile refreshes", () => {
    const { result } = renderHook(useIsCurrentAccount, { wrapper: StrictMode })
    const captured = result.current
    expect(captured()).toBe(true)

    act(() => useAppStore.setState({ user: { ...USER_A, name: "Updated" } }))

    expect(result.current).toBe(captured)
    expect(captured()).toBe(true)
  })

  it("invalidates an old callback immediately even before its component unmounts", () => {
    const { result } = renderHook(useIsCurrentAccount)
    const captured = result.current
    act(() => {
      useAppStore.setState({ user: { ...USER_A, id: "user-b" } })
      expect(captured()).toBe(false)
    })

    expect(result.current()).toBe(true)
    expect(captured()).toBe(false)
  })

  it("does not revive a captured callback when the same account logs back in", () => {
    const { result } = renderHook(useIsCurrentAccount)
    const captured = result.current
    act(() => {
      useAppStore.setState({ user: null })
      useAppStore.setState({ user: USER_A })
    })

    expect(result.current()).toBe(true)
    expect(captured()).toBe(false)
  })

  it("invalidates a callback when its component unmounts without changing accounts", () => {
    const { result, unmount } = renderHook(useIsCurrentAccount)
    const captured = result.current
    unmount()

    expect(captured()).toBe(false)
    expect(useAppStore.getState().user?.id).toBe(USER_A.id)
  })
})
