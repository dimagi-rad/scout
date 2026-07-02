import { render, screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { api } from "@/api/client"
import { useAppStore } from "@/store/store"
import { ArtifactPanel } from "./ArtifactPanel"

const WORKSPACE_ID = "11111111-1111-1111-1111-111111111111"
const ARTIFACT_ID = "22222222-2222-2222-2222-222222222222"

describe("ArtifactPanel", () => {
  beforeEach(() => {
    useAppStore.setState({
      activeDomainId: WORKSPACE_ID,
      activeArtifactId: ARTIFACT_ID,
    })
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it("renders the active artifact in a modal and closes through the viewer toolbar", async () => {
    vi.spyOn(api, "get").mockImplementation(async (url: string) => {
      if (url === `/api/workspaces/${WORKSPACE_ID}/artifacts/${ARTIFACT_ID}/data/`) {
        return {
          id: ARTIFACT_ID,
          title: "Program Overview",
          type: "react",
          code: "export default function Artifact() { return <div /> }",
          data: {},
          semantic_queries: [],
          version: 1,
        } as never
      }
      throw new Error(`Unexpected request: ${url}`)
    })

    render(<ArtifactPanel />)

    expect(await screen.findByTestId("artifact-modal")).toBeInTheDocument()
    expect(await screen.findByText("Program Overview")).toBeInTheDocument()
    expect(screen.getByTestId(`artifact-frame-${ARTIFACT_ID}`)).toHaveAttribute(
      "src",
      `/api/workspaces/${WORKSPACE_ID}/artifacts/${ARTIFACT_ID}/sandbox/`,
    )

    await userEvent.click(screen.getByTestId("artifact-close"))

    await waitFor(() => {
      expect(useAppStore.getState().activeArtifactId).toBeNull()
    })
  })
})
