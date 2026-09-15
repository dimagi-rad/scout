import { render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

import { ArtifactGraphRenderer } from "./ArtifactGraphRenderer"
import type { ArtifactDetail } from "./types"

afterEach(() => vi.unstubAllGlobals())

describe("artifact semantic-query error presentation", () => {
  it("shows the actual API explanation through the real client and graph engine", async () => {
    const message = "Unknown semantic field 'visits.missing_measure'."
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      success: false,
      error: { code: "VALIDATION_ERROR", message, detail: "private diagnostic" },
    }), { status: 400, statusText: "Bad Request" }))
    vi.stubGlobal("fetch", fetchMock)
    const artifact: ArtifactDetail = {
      id: "validation-artifact",
      title: "Visits",
      type: "story",
      code: "",
      data: {
        story_doc: {
          schema_version: 1,
          blocks: [
            { id: "title", type: "title", config: { text: "Visits" } },
            {
              id: "query", type: "semantic_query", hidden: true,
              config: { queries: { visits: { measures: ["visits.missing_measure"] } } },
            },
            {
              id: "table", type: "table", inputs: { data: { $ref: "query.visits" } },
              config: { columns: ["visits_missing_measure"] },
            },
            {
              id: "summary", type: "section",
              config: { title: "Context", text: "This explanation remains readable when a query fails." },
            },
          ],
        },
      },
      semantic_queries: [],
      version: 1,
    }
    const { container } = render(<ArtifactGraphRenderer artifact={artifact} workspaceId="workspace-1" />)

    expect(await screen.findByText(new RegExp("Unknown semantic field"))).toHaveTextContent(message)
    expect(screen.getByRole("heading", { name: "Visits" })).toBeInTheDocument()
    expect(screen.getByText("This explanation remains readable when a query fails.")).toBeInTheDocument()
    expect(container).not.toHaveTextContent("[object Object]")
    expect(container).not.toHaveTextContent("private diagnostic")
    expect(container).not.toHaveTextContent("Loading data")
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(fetchMock).toHaveBeenCalledWith("/api/workspaces/workspace-1/semantic-query/", expect.objectContaining({
      method: "POST", credentials: "include",
    }))
  })
})
