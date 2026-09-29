import { beforeEach, describe, expect, it, vi } from "vitest"
import { render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { MemoryRouter } from "react-router-dom"
import { WorkspaceAddedNotice } from "./WorkspaceAddedNotice"
import { useAppStore } from "@/store/store"

const navigate = vi.fn()
vi.mock("react-router-dom", async (importOriginal) => ({
  ...(await importOriginal<typeof import("react-router-dom")>()),
  useNavigate: () => navigate,
}))

const ws = (id: string, display_name: string) => ({
  id,
  name: id,
  display_name,
  is_auto_created: false,
  role: "read" as const,
  tenants: [],
  has_access: true,
  member_count: 2,
  schema_status: "available" as const,
  last_synced_at: null,
  created_at: "2026-01-01T00:00:00Z",
})

function renderNotice() {
  return render(
    <MemoryRouter>
      <WorkspaceAddedNotice />
    </MemoryRouter>,
  )
}

describe("WorkspaceAddedNotice (#355)", () => {
  beforeEach(() => {
    navigate.mockClear()
    useAppStore.setState({
      domainsStatus: "loaded",
      domains: [ws("home", "Home"), ws("new", "Malaria Study")],
      activeDomainId: "home",
      addedDomainIds: [],
    })
  })

  it("renders nothing when no workspace was added", () => {
    renderNotice()
    expect(screen.getByTestId("workspace-added-notice")).toBeEmptyDOMElement()
  })

  it("tells you about a workspace you were added to and opens it", async () => {
    useAppStore.setState({ addedDomainIds: ["new"] })
    renderNotice()

    expect(screen.getByTestId("workspace-added-notice-new")).toHaveTextContent(
      "You now have access to Malaria Study.",
    )
    await userEvent.click(screen.getByTestId("workspace-added-notice-open-new"))

    expect(navigate).toHaveBeenCalledWith("/workspaces/malaria-study/new/chat")
    expect(useAppStore.getState().addedDomainIds).toEqual([])
    expect(screen.getByTestId("workspace-added-notice")).toBeEmptyDOMElement()
  })

  it("goes away when dismissed", async () => {
    useAppStore.setState({ addedDomainIds: ["new"] })
    renderNotice()

    await userEvent.click(screen.getByTestId("workspace-added-notice-dismiss-new"))

    expect(navigate).not.toHaveBeenCalled()
    expect(screen.getByTestId("workspace-added-notice")).toBeEmptyDOMElement()
  })

  it("skips the workspace you're in and ones no longer listed", () => {
    useAppStore.setState({ addedDomainIds: ["home", "gone"] })
    renderNotice()
    expect(screen.getByTestId("workspace-added-notice")).toBeEmptyDOMElement()
  })
})
