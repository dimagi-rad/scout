import { beforeEach, describe, expect, it, vi } from "vitest"
import { render, screen, waitFor, within } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { personalMemoryApi, workspaceMemoryApi } from "@/api/memory"
import { useAppStore } from "@/store/store"
import { MemoryPage } from "./MemoryPage"

vi.mock("@/api/memory", () => ({
  personalMemoryApi: {
    list: vi.fn(),
    create: vi.fn(),
    update: vi.fn(),
    remove: vi.fn(),
  },
  workspaceMemoryApi: {
    list: vi.fn(),
    create: vi.fn(),
    update: vi.fn(),
    remove: vi.fn(),
  },
}))

const memory = {
  id: "m1",
  content: "Show district totals as a table",
  created_at: "2026-10-01T10:00:00Z",
  updated_at: "2026-10-01T10:00:00Z",
}

const mocked = vi.mocked(personalMemoryApi)
const workspaceMocked = vi.mocked(workspaceMemoryApi)

const workspaceMemory = (overrides: Record<string, unknown> = {}) => ({
  id: "w1",
  content: "Exclude test visits",
  tables: [],
  author_name: "Ana",
  is_mine: false,
  can_edit: false,
  created_at: "2026-10-01T10:00:00Z",
  updated_at: "2026-10-01T10:00:00Z",
  ...overrides,
})

describe("Memory page, personal section", () => {
  beforeEach(() => {
    vi.resetAllMocks()
    useAppStore.setState({ activeDomainId: null })
    mocked.list.mockResolvedValue({ results: [memory], limit: 50 })
  })

  it("lists the user's memories", async () => {
    render(<MemoryPage />)
    expect(await screen.findByTestId("memory-personal-content-m1")).toHaveTextContent(
      "Show district totals as a table",
    )
  })

  it("adds a memory", async () => {
    mocked.create.mockResolvedValue({ ...memory, id: "m2", content: "Use metric units" })
    render(<MemoryPage />)
    await screen.findByTestId("memory-personal-item-m1")

    await userEvent.type(screen.getByTestId("memory-personal-add-input"), "Use metric units")
    await userEvent.click(screen.getByTestId("memory-personal-add"))

    expect(mocked.create).toHaveBeenCalledWith("Use metric units")
    expect(await screen.findByTestId("memory-personal-content-m2")).toHaveTextContent(
      "Use metric units",
    )
  })

  it("says so when the memory is already saved", async () => {
    mocked.create.mockResolvedValue(memory)
    render(<MemoryPage />)
    await screen.findByTestId("memory-personal-item-m1")
    await userEvent.type(
      screen.getByTestId("memory-personal-add-input"),
      "show district totals as a table",
    )
    await userEvent.click(screen.getByTestId("memory-personal-add"))
    expect(await screen.findByRole("alert")).toHaveTextContent("already saved")
  })

  it("edits a memory in place", async () => {
    mocked.update.mockResolvedValue({ ...memory, content: "Show totals as a sorted table" })
    render(<MemoryPage />)
    await userEvent.click(await screen.findByTestId("memory-personal-edit-m1"))
    const input = screen.getByTestId("memory-personal-edit-input-m1")
    await userEvent.clear(input)
    await userEvent.type(input, "Show totals as a sorted table")
    await userEvent.click(screen.getByTestId("memory-personal-save-m1"))

    expect(mocked.update).toHaveBeenCalledWith("m1", "Show totals as a sorted table")
    expect(await screen.findByTestId("memory-personal-content-m1")).toHaveTextContent(
      "Show totals as a sorted table",
    )
  })

  it("deletes a memory after confirming", async () => {
    mocked.remove.mockResolvedValue(undefined)
    render(<MemoryPage />)
    await userEvent.click(await screen.findByTestId("memory-personal-delete-m1"))
    await userEvent.click(screen.getByTestId("memory-personal-confirm-delete"))

    expect(mocked.remove).toHaveBeenCalledWith("m1")
    await waitFor(() =>
      expect(screen.queryByTestId("memory-personal-item-m1")).not.toBeInTheDocument(),
    )
    expect(
      within(screen.getByTestId("memory-personal-section")).getByTestId("memory-personal-empty"),
    ).toBeInTheDocument()
  })

  it("hides the add form until the list loads, and retries a failed load", async () => {
    mocked.list.mockRejectedValueOnce(new Error("boom"))
    render(<MemoryPage />)
    expect(await screen.findByTestId("memory-personal-error")).toBeInTheDocument()
    expect(screen.queryByTestId("memory-personal-add-input")).not.toBeInTheDocument()

    await userEvent.click(screen.getByTestId("memory-personal-retry"))
    expect(await screen.findByTestId("memory-personal-item-m1")).toBeInTheDocument()
    expect(screen.getByTestId("memory-personal-add-input")).toBeInTheDocument()
  })
})

describe("Memory page, workspace section", () => {
  beforeEach(() => {
    vi.resetAllMocks()
    mocked.list.mockResolvedValue({ results: [], limit: 50 })
    useAppStore.setState({ activeDomainId: "ws-1" })
  })

  it("shows every member the workspace's memories, editable only where allowed", async () => {
    workspaceMocked.list.mockResolvedValue({
      results: [
        workspaceMemory(),
        workspaceMemory({ id: "w2", content: "Mine", is_mine: true, can_edit: true }),
      ],
      can_add: true,
      total: 2,
    })
    render(<MemoryPage />)

    expect(await screen.findByTestId("memory-workspace-content-w1")).toHaveTextContent(
      "Exclude test visits",
    )
    expect(workspaceMocked.list).toHaveBeenCalledWith("ws-1", expect.anything())
    expect(screen.queryByTestId("memory-workspace-edit-w1")).not.toBeInTheDocument()
    expect(screen.queryByTestId("memory-workspace-delete-w1")).not.toBeInTheDocument()
    expect(screen.getByTestId("memory-workspace-edit-w2")).toBeInTheDocument()
    expect(screen.getByTestId("memory-workspace-item-w1")).toHaveTextContent("Ana")
  })

  it("hides the add form from read-only members", async () => {
    workspaceMocked.list.mockResolvedValue({
      results: [workspaceMemory()],
      total: 1,
      can_add: false,
    })
    render(<MemoryPage />)

    expect(await screen.findByTestId("memory-workspace-read-only")).toBeInTheDocument()
    expect(screen.queryByTestId("memory-workspace-add-input")).not.toBeInTheDocument()
  })

  it("lets writers add a workspace memory", async () => {
    workspaceMocked.list.mockResolvedValue({ results: [], total: 0, can_add: true })
    workspaceMocked.create.mockResolvedValue(
      workspaceMemory({ id: "w3", content: "Count households once", is_mine: true, can_edit: true }),
    )
    render(<MemoryPage />)

    await userEvent.type(
      await screen.findByTestId("memory-workspace-add-input"),
      "Count households once",
    )
    await userEvent.click(screen.getByTestId("memory-workspace-add"))

    expect(workspaceMocked.create).toHaveBeenCalledWith("ws-1", "Count households once")
    expect(await screen.findByTestId("memory-workspace-content-w3")).toBeInTheDocument()
  })

  it("says how many older memories the list leaves out", async () => {
    workspaceMocked.list.mockResolvedValue({
      results: [workspaceMemory()],
      total: 3,
      can_add: true,
    })
    render(<MemoryPage />)
    expect(await screen.findByTestId("memory-workspace-hidden-count")).toHaveTextContent("2 older")
  })
})
