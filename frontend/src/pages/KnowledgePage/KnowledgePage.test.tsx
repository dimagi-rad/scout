import { render, screen } from "@testing-library/react"
import { MemoryRouter } from "react-router-dom"
import { afterEach, describe, expect, it, vi } from "vitest"

import { api } from "@/api/client"
import type { WorkspaceListItem } from "@/api/workspaces"
import { useAppStore } from "@/store/store"
import { KnowledgePage } from "./KnowledgePage"

const WORKSPACE_ID = "ws-1"
const emptyList = {
  results: [],
  pagination: {
    page: 1,
    page_size: 50,
    total_count: 0,
    total_pages: 1,
    has_next: false,
    has_previous: false,
  },
}

function renderAs(role: WorkspaceListItem["role"]) {
  vi.spyOn(api, "get").mockResolvedValue(emptyList)
  useAppStore.setState({
    activeDomainId: WORKSPACE_ID,
    domains: [{ id: WORKSPACE_ID, role } as WorkspaceListItem],
  })
  render(
    <MemoryRouter initialEntries={["/knowledge"]}>
      <KnowledgePage />
    </MemoryRouter>,
  )
}

afterEach(() => {
  vi.restoreAllMocks()
  useAppStore.setState({ activeDomainId: null, domains: [] })
})

describe("knowledge page upload control", () => {
  it.each([
    ["read", false],
    ["read_write", true],
  ] as const)("%s members see Upload document: %s", async (role, visible) => {
    renderAs(role)

    expect(await screen.findByTestId("knowledge-export")).toBeInTheDocument()
    expect(screen.queryByTestId("knowledge-upload") !== null).toBe(visible)
  })
})
