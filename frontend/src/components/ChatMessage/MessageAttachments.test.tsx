import { fireEvent, render, screen } from "@testing-library/react"
import type { UIMessage } from "ai"
import { MemoryRouter } from "react-router-dom"
import { describe, expect, it, vi } from "vitest"

import { ChatMessage } from "./ChatMessage"

vi.mock("@/config", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/config")>()),
  withBasePath: (path: string) => `/scout${path}`,
}))

const URL_A = "/api/chat/attachments/bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb/"
const URL_B = "/api/chat/attachments/cccccccc-cccc-cccc-cccc-cccccccccccc/"

function renderMessage(parts: UIMessage["parts"]) {
  render(
    <MemoryRouter>
      <ChatMessage message={{ id: "u1", role: "user", parts }} isActiveMessage={false} />
    </MemoryRouter>,
  )
}

describe("ChatMessage file parts (#913)", () => {
  it("shows an image as a thumbnail that opens full size, under the base path", () => {
    renderMessage([
      { type: "text", text: "What does this chart show?" },
      { type: "file", mediaType: "image/png", filename: "shot.png", url: URL_A },
    ])
    const link = screen.getByTestId("chat-message-attachment")
    expect(link).toHaveAttribute("href", `/scout${URL_A}`)
    expect(link).toHaveAttribute("target", "_blank")
    expect(link.querySelector("img")).toHaveAttribute("src", `/scout${URL_A}`)
    expect(screen.getByText("What does this chart show?")).toBeInTheDocument()
  })

  it("shows an attachments-only message from history, which has no text part", () => {
    renderMessage([
      { type: "file", mediaType: "image/png", filename: "shot.png", url: URL_A },
      { type: "file", mediaType: "text/csv", filename: "sites.csv", url: URL_B },
    ])
    expect(screen.getAllByTestId("chat-message-attachment")).toHaveLength(2)
    expect(document.querySelector(".bg-primary")).toBeNull()
  })

  it("falls back to a named chip when the image can't be loaded", () => {
    renderMessage([{ type: "file", mediaType: "image/png", filename: "shot.png", url: URL_A }])
    fireEvent.error(screen.getByRole("img"))
    const chip = screen.getByTestId("chat-message-attachment")
    expect(chip).toHaveAttribute("data-kind", "unavailable")
    expect(chip).toHaveTextContent("shot.png")
    expect(chip).toHaveTextContent("Image unavailable")
    expect(chip).not.toHaveAttribute("href")
  })

  it("shows a data file as a named chip", () => {
    renderMessage([
      { type: "file", mediaType: "application/json", filename: "registry.json", url: URL_B },
    ])
    const chip = screen.getByTestId("chat-message-attachment")
    expect(chip).toHaveTextContent("registry.json")
    expect(chip).toHaveTextContent("JSON")
    expect(chip).toHaveAttribute("href", `/scout${URL_B}`)
  })

  it("draws no empty bubble for an attachments-only message", () => {
    renderMessage([
      { type: "file", mediaType: "image/png", filename: "shot.png", url: URL_A },
      { type: "text", text: "" },
    ])
    expect(document.querySelector(".bg-primary")).toBeNull()
  })

  it("shows a file part with no media type as a plain chip", () => {
    renderMessage([{ filename: "odd.bin", url: URL_B, type: "file" } as never])
    expect(screen.getByTestId("chat-message-attachment")).toHaveTextContent("odd.bin")
  })

  it("never fetches or links a url that is not one of our attachments", () => {
    renderMessage([
      { type: "file", mediaType: "image/png", filename: "x.png", url: "https://evil.example/x.png" },
      { type: "file", mediaType: "image/png", filename: "y.png", url: "data:image/png;base64,AAAA" },
    ])
    const shown = screen.getAllByTestId("chat-message-attachment")
    expect(shown).toHaveLength(2)
    for (const item of shown) {
      expect(item).not.toHaveAttribute("href")
      expect(item.querySelector("img")).toBeNull()
    }
  })
})
