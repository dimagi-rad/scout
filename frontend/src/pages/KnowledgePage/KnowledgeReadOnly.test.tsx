import { render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { describe, expect, it, vi } from "vitest"

import { ApiError } from "@/api/client"
import { READ_ONLY_DENIAL, READ_ONLY_HINT } from "@/hooks/useWorkspaceRole"
import type { KnowledgeEntryItem } from "@/store/knowledgeSlice"
import { KnowledgeForm } from "./KnowledgeForm"
import { KnowledgeList } from "./KnowledgeList"

const entry: KnowledgeEntryItem = {
  id: "entry-1",
  type: "entry",
  title: "Active user",
  content: "A user with a visit in the last 30 days.",
  tags: ["metric"],
  created_at: "2026-09-01T10:00:00Z",
  updated_at: "2026-09-01T10:00:00Z",
}

function renderList(canWrite: boolean) {
  const props = { onEdit: vi.fn(), onDelete: vi.fn() }
  render(
    <KnowledgeList
      items={[entry]}
      filter={null}
      search=""
      onFilterChange={vi.fn()}
      onSearchChange={vi.fn()}
      canWrite={canWrite}
      {...props}
    />,
  )
  return props
}

describe("knowledge for read-only members", () => {
  it("offers View instead of Edit and hides Delete", async () => {
    const { onEdit } = renderList(false)

    expect(screen.queryByTestId("knowledge-delete-entry-1")).not.toBeInTheDocument()
    const view = screen.getByTestId("knowledge-edit-entry-1")
    expect(view).toHaveTextContent("View")

    await userEvent.click(view)
    expect(onEdit).toHaveBeenCalledWith(entry)
  })

  it("keeps Edit and Delete for writers", () => {
    renderList(true)

    expect(screen.getByTestId("knowledge-edit-entry-1")).toHaveTextContent("Edit")
    expect(screen.getByTestId("knowledge-delete-entry-1")).toBeInTheDocument()
  })

  it("opens the entry read-only with no way to save", () => {
    render(
      <KnowledgeForm open onOpenChange={vi.fn()} item={entry} onSave={vi.fn()} readOnly />,
    )

    expect(screen.getByText("View Entry")).toBeInTheDocument()
    expect(screen.getByText(READ_ONLY_HINT)).toBeInTheDocument()
    expect(screen.getByLabelText("Title")).toHaveAttribute("readonly")
    expect(screen.getByLabelText("Content")).toHaveAttribute("readonly")
    expect(screen.queryByRole("button", { name: "Save Changes" })).not.toBeInTheDocument()
  })

  it("explains a role denial when a stale writer saves", async () => {
    const onSave = vi.fn().mockRejectedValue(
      new ApiError(403, "Read-write or manage role required for this operation."),
    )
    render(<KnowledgeForm open onOpenChange={vi.fn()} item={entry} onSave={onSave} />)

    await userEvent.click(screen.getByRole("button", { name: "Save Changes" }))

    expect(await screen.findByText(READ_ONLY_DENIAL)).toBeInTheDocument()
  })
})
