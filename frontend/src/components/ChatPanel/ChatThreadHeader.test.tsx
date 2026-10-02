import { describe, expect, it, vi } from "vitest"
import { fireEvent, render, screen } from "@testing-library/react"

import { ChatThreadHeader } from "./ChatThreadHeader"

describe("ChatThreadHeader", () => {
  it("edits the title in place", async () => {
    const onTitleChange = vi.fn()
    render(
      <ChatThreadHeader
        title="Untitled"
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
        title=""
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

  it("does not rename when the provisional title is left unchanged", () => {
    const onTitleChange = vi.fn()
    render(
      <ChatThreadHeader
        title="What are module completion rates?"
        panelOpen={false}
        panelMode="files"
        onTitleChange={onTitleChange}
        onOpenFiles={() => undefined}
        onOpenCanvas={() => undefined}
      />,
    )

    fireEvent.click(screen.getByTestId("chat-thread-rename"))
    fireEvent.blur(screen.getByTestId("chat-thread-title-input"))

    expect(onTitleChange).not.toHaveBeenCalled()
    expect(screen.getByTestId("chat-thread-title")).toHaveTextContent(
      "What are module completion rates?",
    )
  })

  it("clears a provisional title when the user empties it", () => {
    const onTitleChange = vi.fn()
    render(
      <ChatThreadHeader
        title="Loaded custom title"
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
