import { createRef } from "react"
import { act, render, screen, waitFor } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

import { api } from "@/api/client"
import type { ArtifactDetail } from "@/components/ArtifactGraph"
import { ArtifactCanvas, type ArtifactCanvasHandle } from "./ArtifactCanvas"

const artifact: ArtifactDetail = {
  id: "artifact-one", title: "Synthetic print test", type: "story", code: "", version: 1,
  semantic_queries: [{ name: "count", measures: ["test.count"] }],
  data: {
    story_doc: {
      schema_version: 1,
      blocks: [
        { id: "title", type: "title", config: { text: "Synthetic print test" } },
        {
          id: "query", type: "semantic_query", hidden: true,
          config: { queries: { count: { measures: ["test.count"] } } },
        },
        {
          id: "stat", type: "stat", inputs: { current: { $ref: "query.count" } },
          config: { label: "Current count", value_key: "test_count", format: "number_0" },
        },
        { id: "end", type: "markdown", config: { body: "Final story content" } },
      ],
    },
  },
}

describe("ArtifactCanvas PDF export", () => {
  afterEach(() => {
    window.dispatchEvent(new Event("afterprint"))
    vi.restoreAllMocks()
  })

  it("selects the real rendered story without rerunning recovery or semantic queries", async () => {
    const get = vi.spyOn(api, "get").mockResolvedValue({ status: "ready" })
    const post = vi.spyOn(api, "post").mockResolvedValue({ columns: ["test__count"], rows: [[42]], row_count: 1 })
    const print = vi.spyOn(window, "print").mockImplementation(() => {})
    const ref = createRef<ArtifactCanvasHandle>()
    render(<ArtifactCanvas ref={ref} artifactId={artifact.id} workspaceId="workspace" artifact={artifact} isLoading={false} error={null} />)
    expect(await screen.findByText("42")).toBeInTheDocument()
    const title = screen.getByRole("heading", { name: "Synthetic print test" })

    act(() => ref.current?.exportPdf())
    const target = document.querySelector('[data-scout-print="target"]')
    expect(target).toContainElement(title)
    expect(target).toContainElement(screen.getByText("Final story content"))
    expect(print).toHaveBeenCalledTimes(1)
    act(() => window.dispatchEvent(new Event("afterprint")))
    await waitFor(() => expect(get).toHaveBeenCalledTimes(1))
    expect(post).toHaveBeenCalledTimes(1)
    expect(screen.getByRole("heading", { name: "Synthetic print test" })).toBe(title)
    expect(document.querySelector("[data-scout-print]")).toBeNull()
  })

  it("keeps non-story printing inside the opaque-origin sandbox", () => {
    const print = vi.spyOn(window, "print").mockImplementation(() => {})
    const ref = createRef<ArtifactCanvasHandle>()
    render(<ArtifactCanvas ref={ref} artifactId={artifact.id} workspaceId="workspace" artifact={{ ...artifact, type: "html", semantic_queries: [] }} isLoading={false} error={null} />)
    const frame = screen.getByTitle(artifact.title) as HTMLIFrameElement
    const postMessage = vi.spyOn(frame.contentWindow!, "postMessage").mockImplementation(() => {})

    act(() => ref.current?.exportPdf())

    expect(postMessage).toHaveBeenCalledExactlyOnceWith({ type: "scout-print" }, "*")
    expect(frame).toHaveAttribute("sandbox", "allow-scripts allow-modals")
    expect(frame).toHaveAttribute("src", "/api/workspaces/workspace/artifacts/artifact-one/sandbox/")
    expect(print).not.toHaveBeenCalled()
    expect(document.querySelector("[data-scout-print]")).toBeNull()
  })

  it("prints readable stale-data context with the existing chart without additional queries", async () => {
    const get = vi.spyOn(api, "get").mockResolvedValue({
      status: "failed", queryable: true, recovery_action: "semantic_rebuild",
      message: "Showing the last available data. The latest rebuild did not finish.",
      detail: "Synthetic publication failure", data_revision: "published-one",
    })
    const post = vi.spyOn(api, "post").mockResolvedValue({ columns: ["test__count"], rows: [[42]], row_count: 1 })
    const print = vi.spyOn(window, "print").mockImplementation(() => {})
    const ref = createRef<ArtifactCanvasHandle>()
    render(<ArtifactCanvas ref={ref} artifactId={artifact.id} workspaceId="workspace" artifact={artifact} isLoading={false} error={null} />)
    const value = await screen.findByText("42")
    const warning = screen.getByTestId("artifact-data-warning")
    const title = screen.getByRole("heading", { name: "Synthetic print test" })
    const story = title.closest("[data-artifact-story]")

    act(() => ref.current?.exportPdf())

    const target = document.querySelector('[data-scout-print="target"]')
    expect(target).toContainElement(warning)
    expect(target).toContainElement(story as HTMLElement)
    expect(target).toContainElement(value)
    expect(warning).toHaveTextContent("Synthetic publication failure")
    expect(screen.getByTestId("artifact-data-recover")).toHaveAttribute("data-artifact-recovery-control")
    expect(print).toHaveBeenCalledTimes(1)

    act(() => window.dispatchEvent(new Event("afterprint")))
    expect(get).toHaveBeenCalledTimes(1)
    expect(post).toHaveBeenCalledTimes(1)
    expect(screen.getByTestId("artifact-data-warning")).toBe(warning)
    expect(screen.getByRole("heading", { name: "Synthetic print test" })).toBe(title)
    expect(screen.getByText("42")).toBe(value)
    expect(document.querySelector("[data-scout-print]")).toBeNull()
  })
})
