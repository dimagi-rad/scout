import { render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { describe, expect, it, vi } from "vitest"

import { ArtifactCard } from "./ArtifactCard"
import type { ArtifactSummary } from "@/store/artifactSlice"

const artifact: ArtifactSummary = {
  id: "artifact-1",
  title: "Visit timeline",
  description: "Visits grouped by status and date.",
  artifact_type: "story",
  version: 3,
  has_live_queries: true,
  created_at: "2026-08-18T10:00:00Z",
  updated_at: "2026-08-20T10:00:00Z",
}

function renderCard(overrides: Partial<React.ComponentProps<typeof ArtifactCard>> = {}) {
  const props = {
    artifact,
    onOpen: vi.fn(),
    onUpdate: vi.fn().mockResolvedValue(undefined),
    onDelete: vi.fn().mockResolvedValue(undefined),
    ...overrides,
  }

  render(<ArtifactCard {...props} />)
  return props
}

describe("ArtifactCard", () => {
  it("uses the card surface as the primary open action", async () => {
    const { onOpen } = renderCard()

    expect(screen.getByRole("button", { name: "Open Visit timeline" })).toBeInTheDocument()
    expect(screen.queryByText("Live data")).not.toBeInTheDocument()
    expect(screen.getByText("v3")).toBeInTheDocument()
    expect(screen.queryByRole("button", { name: "Visit timeline" })).not.toBeInTheDocument()

    await userEvent.click(screen.getByText("Visit timeline"))

    expect(onOpen).toHaveBeenCalledOnce()
  })

  it("edits title and description through an explicit action", async () => {
    const { onUpdate } = renderCard()

    await userEvent.click(screen.getByRole("button", { name: "Actions for Visit timeline" }))
    await userEvent.click(screen.getByRole("menuitem", { name: "Edit details" }))

    const titleInput = screen.getByLabelText("Title")
    const descriptionInput = screen.getByLabelText("Description")
    await userEvent.clear(titleInput)
    await userEvent.type(titleInput, "Updated timeline")
    await userEvent.clear(descriptionInput)
    await userEvent.type(descriptionInput, "A clearer description")
    await userEvent.click(screen.getByRole("button", { name: "Save changes" }))

    expect(onUpdate).toHaveBeenCalledWith({
      title: "Updated timeline",
      description: "A clearer description",
    })
  })

  it("requires confirmation before deletion", async () => {
    const { onDelete } = renderCard()

    await userEvent.click(screen.getByRole("button", { name: "Actions for Visit timeline" }))
    await userEvent.click(screen.getByRole("menuitem", { name: "Delete artifact" }))

    expect(onDelete).not.toHaveBeenCalled()
    expect(screen.getByRole("alertdialog")).toHaveTextContent("Delete “Visit timeline”?")

    await userEvent.click(screen.getByRole("button", { name: "Delete artifact" }))

    expect(onDelete).toHaveBeenCalledOnce()
  })
})
