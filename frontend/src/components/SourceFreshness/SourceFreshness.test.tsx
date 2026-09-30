import { render, screen } from "@testing-library/react"
import { describe, expect, it } from "vitest"

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
})
