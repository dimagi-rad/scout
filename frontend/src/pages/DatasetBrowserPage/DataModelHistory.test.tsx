import { render, screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { afterEach, describe, expect, it, vi } from "vitest"

import { api, ApiError } from "@/api/client"
import { DataModelHistory, type DataModelRevision } from "./DataModelHistory"

const WORKSPACE_ID = "11111111-1111-1111-1111-111111111111"
const BASE = `/api/workspaces/${WORKSPACE_ID}/data-model/revisions/`

function revision(overrides: Partial<DataModelRevision>): DataModelRevision {
  return {
    id: "rev-1",
    source: "canvas_commit",
    summary: "Created dataset visit_stats",
    created_at: "2026-09-30T12:00:00Z",
    created_by: { id: "u1", name: "Ada" },
    reverts_id: null,
    undone: false,
    ...overrides,
  }
}

describe("DataModelHistory", () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it("loads only when opened and undoes a revision for writers", async () => {
    let undone = false
    const getSpy = vi.spyOn(api, "get").mockImplementation(async () => ({
      revisions: [revision({ undone })],
      can_undo: true,
    }) as never)
    const postSpy = vi.spyOn(api, "post").mockImplementation(async () => {
      undone = true
      return {} as never
    })
    const onChanged = vi.fn()

    render(<DataModelHistory workspaceId={WORKSPACE_ID} onChanged={onChanged} />)
    expect(getSpy).not.toHaveBeenCalled()

    await userEvent.click(screen.getByTestId("data-model-history-btn"))
    expect(await screen.findByText("Created dataset visit_stats")).toBeInTheDocument()

    await userEvent.click(screen.getByTestId("data-model-history-undo-rev-1"))
    expect(postSpy).not.toHaveBeenCalled()
    await userEvent.click(screen.getByTestId("data-model-history-confirm-rev-1"))

    expect(postSpy).toHaveBeenCalledWith(`${BASE}rev-1/undo/`)
    expect(onChanged).toHaveBeenCalled()
    expect(await screen.findByText("Undone")).toBeInTheDocument()
  })

  it("disarms a pending undo confirmation when the panel closes", async () => {
    vi.spyOn(api, "get").mockResolvedValue({
      revisions: [revision({})],
      can_undo: true,
    } as never)

    render(<DataModelHistory workspaceId={WORKSPACE_ID} onChanged={vi.fn()} />)
    await userEvent.click(screen.getByTestId("data-model-history-btn"))
    await userEvent.click(await screen.findByTestId("data-model-history-undo-rev-1"))
    expect(screen.getByTestId("data-model-history-confirm-rev-1")).toBeInTheDocument()

    await userEvent.click(screen.getByTestId("data-model-history-btn"))
    await userEvent.click(screen.getByTestId("data-model-history-btn"))

    expect(await screen.findByTestId("data-model-history-undo-rev-1")).toBeInTheDocument()
    expect(screen.queryByTestId("data-model-history-confirm-rev-1")).not.toBeInTheDocument()
  })

  it("hides undo from read-only members", async () => {
    vi.spyOn(api, "get").mockResolvedValue({
      revisions: [revision({})],
      can_undo: false,
    } as never)

    render(<DataModelHistory workspaceId={WORKSPACE_ID} onChanged={vi.fn()} />)
    await userEvent.click(screen.getByTestId("data-model-history-btn"))

    expect(await screen.findByText("Created dataset visit_stats")).toBeInTheDocument()
    expect(screen.queryByTestId("data-model-history-undo-rev-1")).not.toBeInTheDocument()
  })

  it("explains a conflicting undo", async () => {
    const getSpy = vi.spyOn(api, "get").mockResolvedValue({
      revisions: [revision({})],
      can_undo: true,
    } as never)
    vi.spyOn(api, "post").mockRejectedValue(
      new ApiError(409, "Part of this revision was changed afterwards.", {
        conflicts: [{ object: "dataset/visit_stats", message: "It was edited afterwards." }],
      }),
    )

    render(<DataModelHistory workspaceId={WORKSPACE_ID} onChanged={vi.fn()} />)
    await userEvent.click(screen.getByTestId("data-model-history-btn"))
    await userEvent.click(await screen.findByTestId("data-model-history-undo-rev-1"))
    await userEvent.click(screen.getByTestId("data-model-history-confirm-rev-1"))

    await waitFor(() => {
      expect(screen.getByTestId("data-model-history-error")).toHaveTextContent(
        "dataset/visit_stats: It was edited afterwards.",
      )
    })
    expect(getSpy).toHaveBeenCalledTimes(2)
  })

  it("drops the previous workspace's revisions when the workspace changes", async () => {
    const OTHER_ID = "22222222-2222-2222-2222-222222222222"
    let resolveOther: (value: never) => void = () => {}
    vi.spyOn(api, "get").mockImplementation((url: string) =>
      url === BASE
        ? Promise.resolve({ revisions: [revision({})], can_undo: true } as never)
        : new Promise((resolve) => {
            resolveOther = resolve
          }),
    )

    const { rerender } = render(
      <DataModelHistory workspaceId={WORKSPACE_ID} onChanged={vi.fn()} />,
    )
    await userEvent.click(screen.getByTestId("data-model-history-btn"))
    expect(await screen.findByText("Created dataset visit_stats")).toBeInTheDocument()
    await userEvent.click(screen.getByTestId("data-model-history-btn"))

    rerender(<DataModelHistory workspaceId={OTHER_ID} onChanged={vi.fn()} />)
    await userEvent.click(screen.getByTestId("data-model-history-btn"))

    expect(await screen.findByText("Loading history")).toBeInTheDocument()
    expect(screen.queryByTestId("data-model-history-undo-rev-1")).not.toBeInTheDocument()
    resolveOther({ revisions: [], can_undo: true } as never)
    expect(await screen.findByText("No saved changes yet.")).toBeInTheDocument()
  })

  it("warns when the undo saved but the query layer was not rebuilt", async () => {
    vi.spyOn(api, "get").mockResolvedValue({
      revisions: [revision({})],
      can_undo: true,
    } as never)
    vi.spyOn(api, "post").mockResolvedValue({
      cube_schema: { ok: false, error: "validator unavailable" },
    } as never)

    render(<DataModelHistory workspaceId={WORKSPACE_ID} onChanged={vi.fn()} />)
    await userEvent.click(screen.getByTestId("data-model-history-btn"))
    await userEvent.click(await screen.findByTestId("data-model-history-undo-rev-1"))
    await userEvent.click(screen.getByTestId("data-model-history-confirm-rev-1"))

    await waitFor(() => {
      expect(screen.getByTestId("data-model-history-error")).toHaveTextContent(
        "validator unavailable",
      )
    })
  })
})
