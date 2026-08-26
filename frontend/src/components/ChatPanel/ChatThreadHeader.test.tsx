import { describe, expect, it, vi } from "vitest"
import { fireEvent, render, screen } from "@testing-library/react"

import { ChatThreadHeader } from "./ChatThreadHeader"

describe("ChatThreadHeader", () => {
  it("edits the title in place", async () => {
    const onTitleChange = vi.fn()
    render(
      <ChatThreadHeader
        title="Untitled"
        titleIsCustom={false}
        panelOpen={false}
        panelMode="files"
        onTitleChange={onTitleChange}
        onOpenFiles={() => undefined}
        onOpenCanvas={() => undefined}
      />,
    )

    fireEvent.click(screen.getByRole("button", { name: "Rename thread" }))
    const input = screen.getByRole("textbox", { name: "Thread title" })
    fireEvent.change(input, { target: { value: "UX test artifact creation" } })
    fireEvent.submit(input.closest("form")!)

    expect(onTitleChange).toHaveBeenCalledWith("UX test artifact creation")
  })

  it("renders the untitled fallback as muted text", () => {
    render(
      <ChatThreadHeader
        title="Untitled"
        titleIsCustom={false}
        panelOpen={false}
        panelMode="files"
        onTitleChange={() => undefined}
        onOpenFiles={() => undefined}
        onOpenCanvas={() => undefined}
      />,
    )

    expect(screen.getByText("Untitled")).toHaveClass("text-muted-foreground")
  })

  it("exposes artifacts and canvas controls", () => {
    const onOpenFiles = vi.fn()
    const onOpenCanvas = vi.fn()
    render(
      <ChatThreadHeader
        title="Artifact work"
        titleIsCustom
        panelOpen
        panelMode="files"
        onTitleChange={() => undefined}
        onOpenFiles={onOpenFiles}
        onOpenCanvas={onOpenCanvas}
      />,
    )

    fireEvent.click(screen.getByRole("button", { name: "Artifacts" }))
    fireEvent.click(screen.getByRole("button", { name: "Canvas" }))

    expect(onOpenFiles).toHaveBeenCalledOnce()
    expect(onOpenCanvas).toHaveBeenCalledOnce()
  })

  it("shortens long displayed titles to 200 characters plus ellipsis", () => {
    const longTitle = `${"a".repeat(205)} tail`
    render(
      <ChatThreadHeader
        title={longTitle}
        titleIsCustom
        panelOpen={false}
        panelMode="files"
        onTitleChange={() => undefined}
        onOpenFiles={() => undefined}
        onOpenCanvas={() => undefined}
      />,
    )

    expect(screen.getByText(`${"a".repeat(200)}...`)).toBeInTheDocument()
    expect(screen.queryByText(longTitle)).not.toBeInTheDocument()
  })

  it("clears a custom title back to the untitled fallback", () => {
    const onTitleChange = vi.fn()
    render(
      <ChatThreadHeader
        title="Custom title"
        titleIsCustom
        panelOpen={false}
        panelMode="files"
        onTitleChange={onTitleChange}
        onOpenFiles={() => undefined}
        onOpenCanvas={() => undefined}
      />,
    )

    fireEvent.click(screen.getByRole("button", { name: "Rename thread" }))
    const input = screen.getByRole("textbox", { name: "Thread title" })
    fireEvent.change(input, { target: { value: "" } })
    fireEvent.submit(input.closest("form")!)

    expect(onTitleChange).toHaveBeenCalledWith("")
  })

  it("clears a non-default displayed title even if custom state is stale", () => {
    const onTitleChange = vi.fn()
    render(
      <ChatThreadHeader
        title="Loaded custom title"
        titleIsCustom={false}
        panelOpen={false}
        panelMode="files"
        onTitleChange={onTitleChange}
        onOpenFiles={() => undefined}
        onOpenCanvas={() => undefined}
      />,
    )

    fireEvent.click(screen.getByRole("button", { name: "Rename thread" }))
    const input = screen.getByRole("textbox", { name: "Thread title" })
    fireEvent.change(input, { target: { value: "" } })
    fireEvent.submit(input.closest("form")!)

    expect(onTitleChange).toHaveBeenCalledWith("")
  })
})
