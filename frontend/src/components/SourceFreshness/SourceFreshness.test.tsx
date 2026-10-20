import { render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { useState } from "react"
import { describe, expect, it } from "vitest"

import type { WorkspaceFreshness } from "@/api/workspaces"

import { freshness, freshSource } from "@/components/StaleDataBanner/testFixtures"
import { SourceFreshness } from "./SourceFreshness"

describe("SourceFreshness", () => {
  it("shows a data-as-of line per source", () => {
    render(
      <SourceFreshness freshness={freshness([freshSource("Alpha", 1), freshSource("Beta", null)])} />,
    )

    expect(screen.getByTestId("source-freshness-Alpha")).toHaveTextContent(
      "Alpha: data as of 1 hour ago",
    )
    expect(screen.getByTestId("source-freshness-Beta")).toHaveTextContent("Beta: not loaded yet")
  })

  it("gives no age for data the workspace does not query", () => {
    render(<SourceFreshness freshness={freshness([freshSource("Alpha", 1, { serving: false })])} />)

    expect(screen.getByTestId("source-freshness-Alpha")).toHaveTextContent(
      "Alpha: loaded but not in use",
    )
  })

  it("renders nothing until freshness arrives", () => {
    render(<SourceFreshness freshness={null} />)

    expect(screen.queryByTestId("source-freshness")).toBeNull()
  })

  it("opens a per-table summary of what the last load put in each source", async () => {
    render(
      <Harness
        freshness={freshness([
          freshSource("Alpha", 1, {
            summary: {
              total_rows: 1234,
              tables: [
                {
                  name: "forms",
                  state: "completed",
                  rows: 1234,
                  earliest: "2025-01-02T10:00:00+00:00",
                  latest: "2026-10-01T00:00:00+00:00",
                },
                { name: "cases", state: "failed", rows: 0 },
              ],
            },
          }),
          freshSource("Beta", null),
        ])}
      />,
    )
    expect(screen.queryByTestId("load-summary")).toBeNull()

    await userEvent.click(screen.getByTestId("source-freshness-details"))

    expect(screen.getByTestId("load-summary-source-Alpha")).toHaveTextContent(
      "1,234 rows in 1 table",
    )
    expect(screen.getByTestId("load-summary-table-Alpha-forms")).toHaveTextContent(
      /forms\s*1,234\s*Jan 2, 2025 – Oct 1, 2026/,
    )
    expect(screen.getByTestId("load-summary-table-Alpha-cases")).toHaveTextContent("failed")
    expect(screen.getByTestId("load-summary-source-Beta")).toHaveTextContent("Not loaded yet.")

    await userEvent.click(screen.getByTestId("load-summary-close"))
    expect(screen.queryByTestId("load-summary")).toBeNull()
  })

  it("offers no details when no source has a load summary", () => {
    render(<Harness freshness={freshness([freshSource("Alpha", 1)])} />)

    expect(screen.queryByTestId("source-freshness-details")).toBeNull()
  })
})

function Harness({ freshness }: { freshness: WorkspaceFreshness }) {
  const [open, setOpen] = useState(false)
  return <SourceFreshness freshness={freshness} summaryOpen={open} onSummaryOpenChange={setOpen} />
}
