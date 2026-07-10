import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { MemoryRouter } from "react-router-dom"
import { LostAccessModal } from "./LostAccessModal"
import { useAppStore } from "@/store/store"

const navigate = vi.fn()
vi.mock("react-router-dom", async (importOriginal) => ({
  ...(await importOriginal<typeof import("react-router-dom")>()),
  useNavigate: () => navigate,
}))

const ws = (id: string, has_access: boolean, provider = "commcare") => ({
  id,
  name: id,
  display_name: id,
  is_auto_created: false,
  role: "manage" as const,
  tenants: [{ id: `t-${id}`, tenant_name: id, provider }],
  has_access,
  member_count: 1,
  schema_status: "available" as const,
  last_synced_at: null,
  created_at: "2026-01-01T00:00:00Z",
})

function renderModal() {
  return render(
    <MemoryRouter>
      <LostAccessModal />
    </MemoryRouter>,
  )
}

describe("LostAccessModal", () => {
  beforeEach(() => {
    navigate.mockClear()
    useAppStore.setState({ domainsStatus: "loaded", domains: [], activeDomainId: null })
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it("does not render while domains are still loading", () => {
    useAppStore.setState({ domainsStatus: "loading", domains: [ws("skelly", false)], activeDomainId: "skelly" })
    renderModal()
    expect(screen.queryByTestId("lost-access-modal")).toBeNull()
  })

  it("does not render when the active workspace still has access", () => {
    useAppStore.setState({ domains: [ws("live", true)], activeDomainId: "live" })
    renderModal()
    expect(screen.queryByTestId("lost-access-modal")).toBeNull()
  })

  it("gates and names the provider when the active workspace lost access", () => {
    useAppStore.setState({
      domains: [ws("skelly", false), ws("live", true)],
      activeDomainId: "skelly",
    })
    renderModal()

    expect(screen.getByTestId("lost-access-modal")).toBeInTheDocument()
    expect(screen.getByText(/lost access to “skelly”/)).toBeInTheDocument()
    expect(screen.getByText("CommCare")).toBeInTheDocument()
    // Only accessible workspaces appear in the picker.
    expect(screen.getByTestId("lost-access-goto-live")).toBeInTheDocument()
    expect(screen.queryByTestId("lost-access-goto-skelly")).toBeNull()
  })

  it("switches to a chosen accessible workspace", async () => {
    useAppStore.setState({
      domains: [ws("skelly", false), ws("live", true)],
      activeDomainId: "skelly",
    })
    renderModal()

    await userEvent.click(screen.getByTestId("lost-access-goto-live"))

    expect(useAppStore.getState().activeDomainId).toBe("live")
    expect(navigate).toHaveBeenCalledWith(expect.stringContaining("/live/chat"))
  })

  it("tells the user when they have no accessible workspaces", () => {
    useAppStore.setState({ domains: [ws("skelly", false)], activeDomainId: "skelly" })
    renderModal()

    expect(screen.getByTestId("lost-access-modal")).toBeInTheDocument()
    expect(screen.queryByTestId("lost-access-picker")).toBeNull()
    expect(screen.getByText(/don’t have access to any workspaces/)).toBeInTheDocument()
  })
})
