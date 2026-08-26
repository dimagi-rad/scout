import { render, screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { MemoryRouter } from "react-router-dom"
import { describe, expect, it } from "vitest"

import { router } from "@/router"

import { ArtifactDemoPage } from "./ArtifactDemoPage"

describe("ArtifactDemoPage", () => {
  it("shows a production-rendered Story artifact and the demo data contract", async () => {
    const { container } = render(
      <MemoryRouter>
        <ArtifactDemoPage />
      </MemoryRouter>,
    )

    expect(screen.getByRole("heading", { name: "Artifact showcase" })).toBeInTheDocument()
    expect(screen.getByText("Community visit operations review")).toBeInTheDocument()
    expect(screen.getByText("Approved visits")).toBeInTheDocument()
    expect(screen.getByLabelText("Date range")).toBeInTheDocument()
    expect(screen.getByLabelText("Comparison period")).toBeInTheDocument()
    expect(screen.getByText(/Same period last year/)).toBeInTheDocument()
    expect(container.querySelectorAll("[data-stat-delta]")).toHaveLength(4)
    expect(screen.queryByText(/plotly/i)).not.toBeInTheDocument()
    await waitFor(() => {
      expect(container.querySelectorAll('[data-block-type="graph"]')).toHaveLength(3)
    })

    await userEvent.click(screen.getByTestId("artifact-view-data"))
    expect(await screen.findByRole("dialog")).toBeInTheDocument()
    expect(screen.getByText("visits_by_status")).toBeInTheDocument()
    expect(screen.getByText("workflow_completion")).toBeInTheDocument()
  })

  it("exposes chart patterns, responsive controls, states, and supported formats", async () => {
    render(
      <MemoryRouter>
        <ArtifactDemoPage />
      </MemoryRouter>,
    )

    await userEvent.click(screen.getByRole("tab", { name: "Chart gallery" }))
    expect(screen.getByRole("heading", { name: "Trend over time" })).toBeInTheDocument()
    expect(screen.getByRole("heading", { name: "Measure and target" })).toBeInTheDocument()

    const fourWeeks = screen.getByRole("button", { name: "4 weeks" })
    await userEvent.click(fourWeeks)
    expect(fourWeeks).toHaveAttribute("aria-pressed", "true")

    await userEvent.click(screen.getByRole("tab", { name: "States & formats" }))
    expect(screen.getByRole("heading", { name: "Data states" })).toBeInTheDocument()
    expect(screen.getByRole("cell", { name: "Story" })).toBeInTheDocument()
    expect(screen.getByRole("cell", { name: "React" })).toBeInTheDocument()
    expect(screen.getByRole("cell", { name: "HTML" })).toBeInTheDocument()
    expect(screen.getByRole("cell", { name: "Markdown" })).toBeInTheDocument()
    expect(screen.getByRole("cell", { name: "SVG" })).toBeInTheDocument()
    expect(screen.queryByText(/plotly/i)).not.toBeInTheDocument()
  })

  it("is registered on the discoverable artifact demo route", () => {
    const appRoute = router.routes.find((route) => route.path === "/")
    expect(appRoute?.children?.some((route) => route.path === "artifacts/demo")).toBe(true)
  })
})
