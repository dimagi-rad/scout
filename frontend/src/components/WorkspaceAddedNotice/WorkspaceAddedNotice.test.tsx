import { beforeEach, describe, expect, it, vi } from "vitest"
import { act, render, screen } from "@testing-library/react"
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
    // Selected before navigating, so the chat route never mounts with the old workspace's thread.
    expect(useAppStore.getState().activeDomainId).toBe("new")
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
  describe("with many added at once", () => {
    const many = Array.from({ length: 5 }, (_, i) => ws(`w${i}`, `Workspace ${i}`))

    beforeEach(() => {
      useAppStore.setState({
        domains: [ws("home", "Home"), ...many],
        addedDomainIds: many.map((w) => w.id),
      })
    })

    it("shows the newest three and offers the rest behind Show more", async () => {
      renderNotice()

      expect(screen.getByTestId("workspace-added-notice-w4")).toBeInTheDocument()
      expect(screen.getByTestId("workspace-added-notice-w2")).toBeInTheDocument()
      expect(screen.queryByTestId("workspace-added-notice-w1")).not.toBeInTheDocument()
      const showMore = screen.getByTestId("workspace-added-notice-show-more")
      expect(showMore).toHaveTextContent("Show 2 more")

      await userEvent.click(showMore)

      expect(screen.getByTestId("workspace-added-notice-w0")).toBeInTheDocument()
      // Same button, so keyboard focus stays put.
      expect(showMore).toHaveTextContent("Show fewer")
      expect(showMore).toHaveFocus()
    })

    it("starts a later burst capped again after the first is cleared one by one", async () => {
      renderNotice()
      await userEvent.click(screen.getByTestId("workspace-added-notice-show-more"))
      await userEvent.click(screen.getByTestId("workspace-added-notice-dismiss-w0"))
      await userEvent.click(screen.getByTestId("workspace-added-notice-dismiss-w1"))

      act(() => useAppStore.setState({ addedDomainIds: many.map((w) => w.id) }))

      expect(screen.queryByTestId("workspace-added-notice-w0")).not.toBeInTheDocument()
      expect(screen.getByTestId("workspace-added-notice-show-more")).toHaveTextContent(
        "Show 2 more",
      )
    })

    it("shows a grant that arrives while earlier notices are still up", () => {
      useAppStore.setState({ addedDomainIds: ["w0", "w1", "w2"] })
      renderNotice()

      act(() => useAppStore.setState({ addedDomainIds: ["w0", "w1", "w2", "w3"] }))

      expect(screen.getByTestId("workspace-added-notice-w3")).toBeInTheDocument()
    })

    it("shows exactly three with Dismiss all but no Show more", () => {
      useAppStore.setState({ addedDomainIds: ["w0", "w1", "w2"] })
      renderNotice()

      expect(screen.getByTestId("workspace-added-notice-w0")).toBeInTheDocument()
      expect(screen.getByTestId("workspace-added-notice-dismiss-all")).toBeInTheDocument()
      expect(screen.queryByTestId("workspace-added-notice-show-more")).not.toBeInTheDocument()
    })

    it("clears every notice with Dismiss all", async () => {
      renderNotice()

      await userEvent.click(screen.getByTestId("workspace-added-notice-dismiss-all"))

      expect(useAppStore.getState().addedDomainIds).toEqual([])
      expect(screen.getByTestId("workspace-added-notice")).toBeEmptyDOMElement()
    })
  })

  it("offers no Dismiss all for a single notice", () => {
    useAppStore.setState({ addedDomainIds: ["new"] })
    renderNotice()
    expect(screen.queryByTestId("workspace-added-notice-dismiss-all")).not.toBeInTheDocument()
  })
})
