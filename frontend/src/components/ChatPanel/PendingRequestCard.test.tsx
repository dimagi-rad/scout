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

    expect(onEdit).toHaveBeenCalledWith("visits by week", 2)
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

  it("keeps saving against the version the edit started from", async () => {
    const onEdit = vi.fn().mockResolvedValue("conflict")
    const { rerender } = render(
      <PendingRequestCard pending={request(["visits?"])} phase="waiting" onEdit={onEdit} />,
    )
    fireEvent.click(screen.getByTestId("pending-request-edit"))
    fireEvent.change(screen.getByTestId("pending-request-edit-text"), { target: { value: "mine" } })

    rerender(
      <PendingRequestCard pending={request(["visits?", "by month"])} phase="waiting" onEdit={onEdit} />,
    )
    await act(async () => fireEvent.click(screen.getByTestId("pending-request-edit-save")))
    await act(async () => fireEvent.click(screen.getByTestId("pending-request-edit-save")))

    expect(onEdit.mock.calls).toEqual([
      ["mine", 1],
      ["mine", 1],
    ])
  })

  it("hands on an open edit when the request starts sending", async () => {
    const onAbandonEdit = vi.fn()
    const { rerender } = render(
      <PendingRequestCard
        pending={request(["visits?"])}
        phase="waiting"
        onEdit={vi.fn()}
        onAbandonEdit={onAbandonEdit}
      />,
    )
    fireEvent.click(screen.getByTestId("pending-request-edit"))
    fireEvent.change(screen.getByTestId("pending-request-edit-text"), { target: { value: "mine" } })

    rerender(
      <PendingRequestCard
        pending={request(["visits?"])}
        phase="answering"
        onEdit={vi.fn()}
        onAbandonEdit={onAbandonEdit}
      />,
    )

    expect(onAbandonEdit).toHaveBeenCalledWith("mine")
    expect(onAbandonEdit).toHaveBeenCalledTimes(1)
    expect(screen.queryByTestId("pending-request-edit-text")).toBeNull()
    expect(screen.getByTestId("pending-request-notice")).toHaveTextContent("message box")
  })

  it("hands on an open edit when the card closes", () => {
    const onAbandonEdit = vi.fn()
    const { unmount } = render(
      <PendingRequestCard
        pending={request(["visits?"])}
        phase="waiting"
        onEdit={vi.fn()}
        onAbandonEdit={onAbandonEdit}
      />,
    )
    fireEvent.click(screen.getByTestId("pending-request-edit"))
    fireEvent.change(screen.getByTestId("pending-request-edit-text"), { target: { value: "mine" } })

    unmount()

    expect(onAbandonEdit).toHaveBeenCalledWith("mine")
  })

  it("holds other changes while one is on its way", async () => {
    let finish: (outcome: "saved") => void = () => {}
    const onRemovePart = vi.fn(() => new Promise<"saved">((resolve) => (finish = resolve)))
    render(
      <PendingRequestCard
        pending={request(["visits?", "by month", "in Kenya"])}
        phase="waiting"
        onRemovePart={onRemovePart}
        onEdit={vi.fn()}
      />,
    )

    await act(async () => fireEvent.click(screen.getByTestId("pending-request-remove-p2")))
    await act(async () => fireEvent.click(screen.getByTestId("pending-request-remove-p3")))

    expect(onRemovePart).toHaveBeenCalledTimes(1)
    expect(screen.getByTestId("pending-request-remove-p3")).toBeDisabled()
    expect(screen.getByTestId("pending-request-edit")).toBeDisabled()
    expect(screen.getByTestId("pending-request-discard-waiting")).toBeDisabled()
    await act(async () => finish("saved"))
    expect(screen.getByTestId("pending-request-remove-p3")).toBeEnabled()
  })

  it("keeps an edit open while its save is on its way", async () => {
    let finish: (outcome: "conflict") => void = () => {}
    const onEdit = vi.fn(() => new Promise<"conflict">((resolve) => (finish = resolve)))
    render(<PendingRequestCard pending={request(["visits?"])} phase="waiting" onEdit={onEdit} />)
    fireEvent.click(screen.getByTestId("pending-request-edit"))
    fireEvent.change(screen.getByTestId("pending-request-edit-text"), { target: { value: "mine" } })

    await act(async () => fireEvent.click(screen.getByTestId("pending-request-edit-save")))

    expect(screen.getByTestId("pending-request-edit-cancel")).toBeDisabled()
    await act(async () => finish("conflict"))
    expect(screen.getByTestId("pending-request-edit-text")).toHaveValue("mine")
  })
})
