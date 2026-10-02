import { act, fireEvent, render, screen } from "@testing-library/react"
import { describe, expect, it, vi } from "vitest"

import type { PendingRequest } from "@/api/jobs"
import { PendingRequestCard } from "./PendingRequestCard"

function request(texts: string[], version = texts.length): PendingRequest {
  return {
    thread_id: "t",
    request_id: "r",
    version,
    parts: texts.map((text, index) => ({ id: `p${index + 1}`, text, added_at: "" })),
    state: "waiting",
    thread_job_id: "j",
    thread_job_state: "pending",
  }
}

describe("PendingRequestCard", () => {
  it("offers removal only for later parts while waiting", () => {
    render(
      <PendingRequestCard
        pending={request(["visits?", "by month"])}
        phase="waiting"
        onRemovePart={vi.fn()}
        onEdit={vi.fn()}
      />,
    )

    expect(screen.queryByTestId("pending-request-remove-p1")).toBeNull()
    expect(screen.getByTestId("pending-request-remove-p2")).toBeInTheDocument()
  })

  it("offers no changes once the request is being answered", () => {
    render(
      <PendingRequestCard
        pending={request(["visits?", "by month"])}
        phase="answering"
        onRemovePart={vi.fn()}
        onEdit={vi.fn()}
      />,
    )

    expect(screen.queryByTestId("pending-request-remove-p2")).toBeNull()
    expect(screen.queryByTestId("pending-request-edit")).toBeNull()
  })

  it("removes a part", async () => {
    const onRemovePart = vi.fn().mockResolvedValue("saved")
    render(
      <PendingRequestCard
        pending={request(["visits?", "by month"])}
        phase="waiting"
        onRemovePart={onRemovePart}
      />,
    )

    await act(async () => fireEvent.click(screen.getByTestId("pending-request-remove-p2")))

    expect(onRemovePart).toHaveBeenCalledWith("p2")
  })

  it("edits the whole request as one text", async () => {
    const onEdit = vi.fn().mockResolvedValue("saved")
    render(
      <PendingRequestCard pending={request(["visits?", "by month"])} phase="waiting" onEdit={onEdit} />,
    )

    fireEvent.click(screen.getByTestId("pending-request-edit"))
    const box = screen.getByTestId("pending-request-edit-text")
    expect(box).toHaveValue("visits?\n\nby month")
    fireEvent.change(box, { target: { value: "visits by week" } })
    await act(async () => fireEvent.click(screen.getByTestId("pending-request-edit-save")))

    expect(onEdit).toHaveBeenCalledWith("visits by week")
    expect(screen.queryByTestId("pending-request-edit-text")).toBeNull()
  })

  it("says when another tab changed it first, keeping the edit open", async () => {
    const onEdit = vi.fn().mockResolvedValue("conflict")
    render(<PendingRequestCard pending={request(["visits?"])} phase="waiting" onEdit={onEdit} />)

    fireEvent.click(screen.getByTestId("pending-request-edit"))
    fireEvent.change(screen.getByTestId("pending-request-edit-text"), { target: { value: "mine" } })
    await act(async () => fireEvent.click(screen.getByTestId("pending-request-edit-save")))

    expect(screen.getByTestId("pending-request-notice")).toHaveTextContent("Updated in another tab")
    expect(screen.getByTestId("pending-request-edit-text")).toHaveValue("mine")
  })

  it("slides in only the parts added after it showed, and only with motion allowed", () => {
    const { rerender } = render(<PendingRequestCard pending={request(["visits?"])} phase="waiting" />)

    rerender(<PendingRequestCard pending={request(["visits?", "by month"])} phase="waiting" />)

    expect(screen.getByTestId("pending-request-part-p1").className).not.toContain("animate-in")
    expect(screen.getByTestId("pending-request-part-p2").className).toContain(
      "motion-safe:animate-in",
    )
  })
})
