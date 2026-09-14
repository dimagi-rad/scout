import { act, render, waitFor } from "@testing-library/react"
import { describe, expect, it, vi } from "vitest"

import { api } from "@/api/client"
import { ConnectionsPage } from "./ConnectionsPage"

vi.mock("@/api/client", () => ({ api: { get: vi.fn() } }))

describe("ConnectionsPage", () => {
  it("waits for provider refresh before reading connection health", async () => {
    let completeRefresh!: (value: { providers: [] }) => void
    const refreshed = new Promise<{ providers: [] }>((resolve) => { completeRefresh = resolve })
    vi.mocked(api.get).mockImplementation((path) => {
      if (path === "/api/auth/providers/") return refreshed
      return Promise.resolve([])
    })
    render(<ConnectionsPage />)
    expect(api.get).toHaveBeenCalledWith("/api/auth/providers/")
    expect(api.get).not.toHaveBeenCalledWith("/api/auth/connections/")
    await act(async () => { completeRefresh({ providers: [] }) })
    await waitFor(() => expect(api.get).toHaveBeenCalledWith("/api/auth/connections/"))
  })
})
