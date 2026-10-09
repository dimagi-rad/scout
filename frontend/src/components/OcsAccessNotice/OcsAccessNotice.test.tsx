import { render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { MemoryRouter } from "react-router-dom"
import { beforeEach, describe, expect, it, vi } from "vitest"

import { api } from "@/api/client"
import type { User } from "@/store/authSlice"
import { useAppStore } from "@/store/store"
import { OcsAccessNotice } from "./OcsAccessNotice"

vi.mock("@/api/client", () => ({
  api: { get: vi.fn(), post: vi.fn() },
  ApiError: class extends Error {},
  getCsrfToken: () => "tok",
}))

const baseUser: User = {
  id: "u1",
  email: "u@example.com",
  name: "U",
  is_staff: false,
  onboarding_complete: true,
}

function renderNotice(ocs_access_denied: User["ocs_access_denied"], showConnectionsLink = false) {
  useAppStore.setState({ user: { ...baseUser, ocs_access_denied } })
  return render(
    <MemoryRouter>
      <OcsAccessNotice showConnectionsLink={showConnectionsLink} />
    </MemoryRouter>,
  )
}

describe("OcsAccessNotice (SCOUT-DJANGO-3E)", () => {
  beforeEach(() => {
    vi.mocked(api.post).mockReset().mockResolvedValue(undefined)
  })

  it("renders nothing when OCS did not refuse access", () => {
    renderNotice(null)
    expect(screen.queryByTestId("ocs-access-notice")).toBeNull()
  })

  it("names the refused team and links to connected accounts", () => {
    renderNotice({ team: { slug: "acme", name: "Acme Health" } }, true)
    expect(screen.getByTestId("ocs-access-notice").textContent).toContain("Acme Health")
    expect(screen.getByTestId("ocs-access-notice-connections").getAttribute("href")).toBe(
      "/settings/connections",
    )
  })

  it("still explains the refusal when the identity has no team", () => {
    renderNotice({ team: null })
    expect(screen.getByTestId("ocs-access-notice").textContent).toContain("your chatbots")
    expect(screen.queryByTestId("ocs-access-notice-connections")).toBeNull()
  })

  it("hides at once on dismiss and tells the server", async () => {
    renderNotice({ team: { slug: "acme", name: "Acme Health" } })
    await userEvent.click(screen.getByTestId("ocs-access-notice-dismiss"))

    expect(screen.queryByTestId("ocs-access-notice")).toBeNull()
    expect(api.post).toHaveBeenCalledWith("/api/auth/ocs/access-notice/dismiss/")
  })
})
