import { render, screen } from "@testing-library/react"
import { describe, expect, it, vi } from "vitest"

import { api } from "@/api/client"

import { ArtifactGraphRenderer } from "./ArtifactGraphRenderer"
import { buildStoryRegistry } from "./blocks"
import { previousPeriod } from "./runtime"
import type { ArtifactDetail, CompareRanges, DateRange } from "./types"

vi.mock("@/api/client", () => ({
  api: {
    post: vi.fn(),
  },
}))

function artifactWithBlocks(blocks: Array<Record<string, unknown>>): ArtifactDetail {
  return {
    id: "artifact-comparisons",
    title: "Comparison runtime",
    type: "story",
    code: "",
    data: {
      story_doc: {
        schema_version: 1,
        blocks,
      },
    },
    semantic_queries: [],
    version: 1,
  }
}

function periodOutputs(config: Record<string, unknown>) {
  const spec = buildStoryRegistry().get("period_selector")
  return spec?.initialOutputs?.(config) as {
    current: DateRange
    previous: DateRange
    pair: CompareRanges
  }
}

describe("story artifact comparisons", () => {
  it("shows an absolute stat delta from the bound previous rows in the stat format", () => {
    render(
      <ArtifactGraphRenderer
        artifact={artifactWithBlocks([
          {
            id: "revenue",
            type: "stat",
            inputs: {
              current: { value: [{ amount: 125 }] },
              previous: { value: [{ amount: 100 }] },
            },
            config: { label: "Revenue", value_key: "amount", format: "currency_2" },
          },
        ])}
        workspaceId="workspace-1"
      />,
    )

    expect(screen.getByText("$125.00")).toBeInTheDocument()
    expect(screen.getByLabelText("Increased by $25.00 from previous value $100.00; neutral outcome")).toBeInTheDocument()
    expect(screen.getByText("Neutral")).toBeInTheDocument()
    expect(vi.mocked(api.post)).not.toHaveBeenCalled()
  })

  it("uses delta_path when the query returns an explicit comparison delta", () => {
    render(
      <ArtifactGraphRenderer
        artifact={artifactWithBlocks([
          {
            id: "visits",
            type: "stat",
            inputs: { current: { value: [{ count: 82, change: -4 }] } },
            config: { label: "Visits", value_key: "count", delta_path: "[0].change" },
          },
        ])}
        workspaceId="workspace-1"
      />,
    )

    expect(screen.getByText("82")).toBeInTheDocument()
    expect(screen.getByLabelText("Decreased by 4; neutral outcome")).toBeInTheDocument()
  })

  it("shows a percent comparison with semantic favorable direction and caption", () => {
    render(
      <ArtifactGraphRenderer
        artifact={artifactWithBlocks([
          {
            id: "pending",
            type: "stat",
            inputs: {
              current: { value: [{ count: 80 }] },
              previous: { value: [{ count: 100 }] },
            },
            config: {
              label: "Pending review",
              value_key: "count",
              format: "number_0",
              comparison: {
                type: "percent",
                format: "percent_1",
                label: "vs same period last year",
                goal: "lower",
              },
            },
          },
        ])}
        workspaceId="workspace-1"
      />,
    )

    expect(screen.getByText("−20.0%")).toBeInTheDocument()
    const delta = screen.getByLabelText(
      "Decreased by 20.0% from previous value 100; favorable outcome; vs same period last year",
    )
    expect(delta).toHaveTextContent("vs same period last year")
    expect(screen.getByText("Favorable")).toBeInTheDocument()
    expect(delta.firstElementChild).toHaveClass(
      "text-emerald-700",
    )
  })

  it("omits percent comparison when the baseline is zero", () => {
    const { container } = render(
      <ArtifactGraphRenderer
        artifact={artifactWithBlocks([
          {
            id: "visits",
            type: "stat",
            inputs: {
              current: { value: [{ count: 12 }] },
              previous: { value: [{ count: 0 }] },
            },
            config: {
              label: "Visits",
              value_key: "count",
              comparison: { type: "percent", goal: "higher" },
            },
          },
        ])}
        workspaceId="workspace-1"
      />,
    )

    expect(container.querySelector("[data-stat-delta]")).not.toBeInTheDocument()
  })

  it("uses the configured previous_year comparison range", () => {
    const { current, previous, pair } = periodOutputs({
      default_range: "last_7_days",
      default_comparison: "previous_year",
    })

    expect(previous).toEqual({
      start: `${Number(current.start.slice(0, 4)) - 1}${current.start.slice(4)}`,
      end: `${Number(current.end.slice(0, 4)) - 1}${current.end.slice(4)}`,
      preset: "previous_year",
    })
    expect(pair).toEqual({ current, previous, label: "Same period last year" })
  })

  it("defaults to the previous period when no comparison is configured", () => {
    const { current, previous, pair } = periodOutputs({ default_range: "last_7_days" })

    expect(previous).toEqual(previousPeriod(current))
    expect(pair).toEqual({ current, previous, label: "Previous period" })
  })
})
