import { render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { api } from "@/api/client"
import type { WorkspaceListItem } from "@/api/workspaces"
import { useAppStore } from "@/store/store"
import { ArtifactDataDownload } from "./ArtifactDataDownload"

const ARTIFACT_ID = "22222222-2222-2222-2222-222222222222"
const WORKSPACE_ID = "11111111-1111-1111-1111-111111111111"
const base = `/api/workspaces/${WORKSPACE_ID}/artifacts/${ARTIFACT_ID}/data-export/`

function setRole(role: WorkspaceListItem["role"]) {
  useAppStore.setState({ domains: [{ id: WORKSPACE_ID, role } as WorkspaceListItem] })
}

describe("ArtifactDataDownload", () => {
  beforeEach(() => {
    URL.createObjectURL = vi.fn(() => "blob:csv")
    URL.revokeObjectURL = vi.fn()
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it("is hidden from read-only members", () => {
    setRole("read")
    render(<ArtifactDataDownload artifactId={ARTIFACT_ID} workspaceId={WORKSPACE_ID} />)
    expect(screen.queryByTestId("artifact-download-data")).not.toBeInTheDocument()
  })

  it("lists datasets, downloads one, and says when the row cap was hit", async () => {
    setRole("read_write")
    const user = userEvent.setup()
    const get = vi.spyOn(api, "get").mockResolvedValue({
      datasets: [
        { name: "by_region", source: "query" },
        { name: "targets", source: "static" },
      ],
      row_limit: 50000,
    })
    const download = vi.spyOn(api, "download").mockResolvedValue({
      blob: new Blob(["a,b\r\n"]),
      headers: new Headers({
        "Content-Disposition": 'attachment; filename="visits-by-region-first-50000-rows.csv"',
        "X-Scout-Export-Truncated": "true",
        "X-Scout-Export-Row-Limit": "50000",
      }),
    })
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {})

    render(<ArtifactDataDownload artifactId={ARTIFACT_ID} workspaceId={WORKSPACE_ID} />)
    await user.click(screen.getByTestId("artifact-download-data"))
    expect(get).toHaveBeenCalledWith(base)
    await user.click(await screen.findByTestId("artifact-download-dataset-by_region"))

    expect(download).toHaveBeenCalledWith(`${base}csv/?query=by_region`, undefined)
    expect(click).toHaveBeenCalled()
    expect(await screen.findByTestId("artifact-download-notice")).toHaveTextContent(
      "Only the first 50,000 rows",
    )
  })

  it("sends the viewer's date controls with a story export", async () => {
    setRole("manage")
    const user = userEvent.setup()
    const runtime = { as_of: "2026-09-16T13:00:00Z", timezone: "UTC", sources: {} }
    const post = vi.spyOn(api, "post").mockResolvedValue({
      datasets: [{ name: "q.sessions", source: "query" }],
      row_limit: 50000,
    })
    const download = vi.spyOn(api, "download").mockResolvedValue({
      blob: new Blob([""]),
      headers: new Headers({ "X-Scout-Export-Truncated": "false" }),
    })
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {})

    render(<ArtifactDataDownload artifactId={ARTIFACT_ID} workspaceId={WORKSPACE_ID} runtime={runtime} />)
    await user.click(screen.getByTestId("artifact-download-data"))
    await user.click(await screen.findByTestId("artifact-download-dataset-q.sessions"))

    expect(post).toHaveBeenCalledWith(base, runtime)
    expect(download).toHaveBeenCalledWith(`${base}csv/?query=q.sessions`, runtime)
    expect(screen.queryByTestId("artifact-download-notice")).not.toBeInTheDocument()
  })
})
