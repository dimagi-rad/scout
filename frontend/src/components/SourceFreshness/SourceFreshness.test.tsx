import { render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

import { workspaceApi } from "@/api/workspaces"
import { freshness, freshSource } from "@/components/StaleDataBanner/testFixtures"
import { SourceFreshness } from "./SourceFreshness"

afterEach(() => vi.restoreAllMocks())

describe("SourceFreshness", () => {
  it("shows a data-as-of line per source", async () => {
    vi.spyOn(workspaceApi, "getFreshness").mockResolvedValue(
      freshness([freshSource("Alpha", 1), freshSource("Beta", null)]),
    )
    render(<SourceFreshness workspaceId="ws-1" />)

    expect(await screen.findByTestId("source-freshness-Alpha")).toHaveTextContent(
      "Alpha: data as of 1 hour ago",
    )
    expect(screen.getByTestId("source-freshness-Beta")).toHaveTextContent("Beta: not loaded yet")
  })

  it("renders nothing when the request fails", async () => {
    const spy = vi.spyOn(workspaceApi, "getFreshness").mockRejectedValue(new Error("boom"))
    render(<SourceFreshness workspaceId="ws-1" />)

    await vi.waitFor(() => expect(spy).toHaveBeenCalled())
    expect(screen.queryByTestId("source-freshness")).toBeNull()
  })
})
