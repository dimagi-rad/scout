import { render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

import { workspaceApi, type WorkspaceDetail } from "@/api/workspaces"
import { SourceFreshness } from "./SourceFreshness"

function detail(sources: WorkspaceDetail["sources"]): WorkspaceDetail {
  return { id: "ws-1", sources } as WorkspaceDetail
}

afterEach(() => vi.restoreAllMocks())

describe("SourceFreshness", () => {
  it("shows a data-as-of line per source", async () => {
    const hourAgo = new Date(Date.now() - 3600_000).toISOString()
    vi.spyOn(workspaceApi, "getDetail").mockResolvedValue(
      detail([
        { tenant_id: "t1", tenant_name: "Alpha", provider: "commcare", last_synced_at: hourAgo },
        { tenant_id: "t2", tenant_name: "Beta", provider: "commcare", last_synced_at: null },
      ]),
    )
    render(<SourceFreshness workspaceId="ws-1" />)

    expect(await screen.findByTestId("source-freshness-t1")).toHaveTextContent(
      "Alpha: data as of 1 hour ago",
    )
    expect(screen.getByTestId("source-freshness-t2")).toHaveTextContent("Beta: not loaded yet")
  })

  it("renders nothing when the request fails", async () => {
    const spy = vi.spyOn(workspaceApi, "getDetail").mockRejectedValue(new Error("boom"))
    render(<SourceFreshness workspaceId="ws-1" />)

    await vi.waitFor(() => expect(spy).toHaveBeenCalled())
    expect(screen.queryByTestId("source-freshness")).toBeNull()
  })
})
