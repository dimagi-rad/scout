import { describe, expect, it, vi } from "vitest"
import { fireEvent, render, screen } from "@testing-library/react"

import { ChatThreadSidePanel, type ThreadArtifactSummary } from "./ChatThreadSidePanel"

const artifact: ThreadArtifactSummary = {
  id: "086f1caa-0675-4ff4-b113-21580b5602bf",
  title: "Module Completion Snapshot",
  description: "Completion rates by worker with a summary KPI and supporting table.",
  artifact_type: "story",
  version: 2,
  source: "created",
  created_at: "2026-07-01T14:10:00Z",
  updated_at: "2026-07-01T14:15:00Z",
  linked_at: "2026-07-01T14:10:00Z",
  last_seen_at: "2026-07-01T14:15:00Z",
}

describe("ChatThreadSidePanel", () => {
  it("renders artifacts as list items without low-value metadata", () => {
    const onOpenArtifact = vi.fn()
    render(
      <ChatThreadSidePanel
        open
        mode="files"
        artifacts={[artifact]}
        filesStatus="loaded"
        filesError={null}
        canvas={null}
        onClose={() => undefined}
        onOpenArtifact={onOpenArtifact}
        onRefreshFiles={() => undefined}
      />,
    )

    expect(screen.getByText("Module Completion Snapshot")).toBeInTheDocument()
    expect(screen.getByText("Completion rates by worker with a summary KPI and supporting table.")).toBeInTheDocument()
    expect(screen.queryByText("story")).not.toBeInTheDocument()
    expect(screen.queryByText("v2")).not.toBeInTheDocument()
    expect(screen.queryByText("Created")).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole("button", { name: /Module Completion Snapshot/ }))
    expect(onOpenArtifact).toHaveBeenCalledWith("086f1caa-0675-4ff4-b113-21580b5602bf")
  })
})
