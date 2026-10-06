import { beforeEach, describe, expect, it, vi } from "vitest"
import { render, screen, waitFor, within } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { personalMemoryApi } from "@/api/memory"
import { MemoryPage } from "./MemoryPage"

vi.mock("@/api/memory", () => ({
  personalMemoryApi: {
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

describe("Memory page, personal section", () => {
  beforeEach(() => {
    vi.resetAllMocks()
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

  it("says so when the list fails to load", async () => {
    mocked.list.mockRejectedValue(new Error("boom"))
    render(<MemoryPage />)
    expect(await screen.findByTestId("memory-personal-error")).toBeInTheDocument()
  })
})
