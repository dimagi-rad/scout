import { useState, type ReactNode } from "react"
import { act, cleanup, render, screen } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { useAppStore } from "@/store/store"
import type { User } from "@/store/authSlice"
import App from "./App"

function RouteProbe() {
  const [owner] = useState(() => useAppStore.getState().user?.id)
  return <div data-testid="local-state-owner">{owner}</div>
}

vi.mock("react-router-dom", async (importOriginal) => ({
  ...await importOriginal<typeof import("react-router-dom")>(),
  createBrowserRouter: () => ({}),
  RouterProvider: () => <RouteProbe />,
}))
vi.mock("@/router", () => ({ router: {} }))
vi.mock("@/contexts/NetworkStatusContext", () => ({
  NetworkStatusProvider: ({ children }: { children: ReactNode }) => <>{children}</>,
}))
vi.mock("@/hooks/useEmbedMessaging", () => ({
  useEmbedMessaging: () => ({ sendEvent: vi.fn() }),
}))

const user = (id: string): User => ({
  id, email: `${id}@example.invalid`, name: id, is_staff: false, onboarding_complete: true,
})
const initialAuthActions = useAppStore.getState().authActions

beforeEach(() => {
  useAppStore.setState({
    user: user("user-a"), authStatus: "authenticated",
    authActions: { ...initialAuthActions, fetchMe: vi.fn(async () => undefined) },
  })
})
afterEach(() => {
  cleanup()
  useAppStore.setState({ user: null, authStatus: "idle", authActions: initialAuthActions })
  window.history.replaceState({}, "", "/")
  vi.restoreAllMocks()
})

describe.each(["/artifacts", "/embed/artifacts"])("account-local component state at %s", (path) => {
  it("remounts route-local state on a direct authenticated identity change", () => {
    window.history.replaceState({}, "", path)
    render(<App />)
    expect(screen.getByTestId("local-state-owner")).toHaveTextContent("user-a")

    act(() => useAppStore.setState({ user: user("user-b"), authStatus: "authenticated" }))

    expect(screen.getByTestId("local-state-owner")).toHaveTextContent("user-b")
  })

  it("preserves route-local state when the same user's metadata changes", () => {
    window.history.replaceState({}, "", path)
    render(<App />)
    const route = screen.getByTestId("local-state-owner")

    act(() => useAppStore.setState({ user: { ...user("user-a"), name: "Updated name" } }))

    expect(screen.getByTestId("local-state-owner")).toBe(route)
  })
})
