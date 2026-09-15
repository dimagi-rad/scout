import { readFileSync } from "node:fs"
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { RechartsFrame } from "@/components/ArtifactGraph/recharts"
import { useArtifactPrint } from "./useArtifactPrint"

const printCss = readFileSync(`${import.meta.dirname}/artifactPrint.css`, "utf8")
const testStyles: HTMLStyleElement[] = []

function addStyle(css: string) {
  const style = document.createElement("style")
  style.textContent = css
  document.head.append(style)
  testStyles.push(style)
  return style
}

/** jsdom does not implement print media or pagination. Exercise the real print
 * declaration cascade here; browser inspection remains the pixel-layout gate. */
function applyPrintDeclarations(ignoreImportance = false) {
  const source = addStyle(printCss)
  const media = Array.from(source.sheet!.cssRules).find(
    (rule): rule is CSSMediaRule => rule instanceof CSSMediaRule && rule.conditionText === "print",
  )
  expect(media).toBeDefined()
  const declarations = Array.from(media!.cssRules, (rule) => rule.cssText).join("\n")
  addStyle(ignoreImportance ? declarations.replaceAll("!important", "") : declarations)
  return media!
}

function ChartFixture({ pageWidth }: { pageWidth: number }) {
  const { printRef, printArtifact } = useArtifactPrint("chart-print")
  return (
    <main style={{ width: pageWidth }}>
      <button onClick={printArtifact}>Export chart</button>
      <div ref={printRef}>
        <RechartsFrame
          height={280}
          rows={[{ category: "First", count: 4 }, { category: "Second", count: 2 }]}
          tree={{ type: "BarChart", children: [{ type: "Bar", props: { dataKey: "count" } }] }}
        />
        <p>Content after the chart</p>
      </div>
    </main>
  )
}

describe("artifact print CSS against real renderer DOM", () => {
  beforeEach(() => {
    vi.spyOn(window, "print").mockImplementation(() => {})
    vi.stubGlobal("ResizeObserver", class {
      observe() {}
      unobserve() {}
      disconnect() {}
    })
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue({
      x: 0, y: 0, width: 1694, height: 280,
      top: 0, left: 0, right: 1694, bottom: 280, toJSON: () => ({}),
    })
  })

  afterEach(() => {
    window.dispatchEvent(new Event("afterprint"))
    for (const style of testStyles.splice(0)) style.remove()
    document.body.removeAttribute("data-scroll-locked")
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
  })

  it.each([360, 760])("keeps the real Recharts zero-sized intermediary responsive on a %spx print page", async (pageWidth) => {
    const { container } = render(<ChartFixture pageWidth={pageWidth} />)
    await waitFor(() => expect(container.querySelector(".recharts-surface")).not.toBeNull())
    const sizingDiv = container.querySelector<HTMLElement>(".recharts-responsive-container > div")!
    const wrapper = container.querySelector<HTMLElement>(".recharts-wrapper")!
    const svg = container.querySelector<SVGElement>(".recharts-surface")!
    const originalSvg = svg.outerHTML

    // This exact Recharts 3 DOM caused the production-browser regression:
    // max-width:100% alone constrained the 1694px chart to a zero-width parent.
    expect(sizingDiv.style.width).toBe("0px")
    expect(sizingDiv.style.height).toBe("0px")
    expect(wrapper.style.width).toBe("1694px")
    expect(window.getComputedStyle(sizingDiv).width).toBe("0px")
    fireEvent.click(screen.getByText("Export chart"))
    applyPrintDeclarations()

    expect(window.getComputedStyle(sizingDiv).width).toBe("100%")
    expect(window.getComputedStyle(sizingDiv).height).toBe("100%")
    expect(window.getComputedStyle(wrapper).maxWidth).toBe("100%")
    expect(window.getComputedStyle(svg).maxWidth).toBe("100%")
    expect(container.querySelector(".recharts-surface")).toBe(svg)
    expect(svg.outerHTML).toBe(originalSvg)
    expect(screen.getByText("Content after the chart")).toBeInTheDocument()

    act(() => window.dispatchEvent(new Event("afterprint")))
    expect(window.getComputedStyle(sizingDiv).width).toBe("0px")
    expect(window.getComputedStyle(wrapper).maxWidth).not.toBe("100%")
    expect(container.querySelector(".recharts-surface")).toBe(svg)
  })

  it("gives print resets higher specificity than Radix body locks and preserves the normal scroll lock", () => {
    render(<ChartFixture pageWidth={760} />)
    document.body.setAttribute("data-scroll-locked", "1")
    fireEvent.click(screen.getByText("Export chart"))
    // jsdom 29 ignores specificity when two declarations are both !important.
    // Compare the equal-priority cascade without those flags, and independently
    // assert the shipped reset declarations are important. The browser gate
    // verifies the actual !important cascade and rendered modal layout.
    const printRules = applyPrintDeclarations(true)
    const resetRule = Array.from(printRules.cssRules).find((rule): rule is CSSStyleRule => (
      rule instanceof CSSStyleRule && document.body.matches(rule.selectorText)
      && rule.style.getPropertyValue("overflow") === "visible"
    ))!
    expect(resetRule.style.getPropertyPriority("overflow")).toBe("important")
    expect(resetRule.style.getPropertyPriority("position")).toBe("important")
    // Match react-remove-scroll's specificity and late style injection, not an
    // inline mock: !important alone did not override these in the real modal.
    addStyle(`body[data-scroll-locked] {
      overflow: hidden;
      position: relative;
    }`)

    const printStyle = window.getComputedStyle(document.body)
    expect(printStyle.overflow).toBe("visible")
    expect(printStyle.position).toBe("static")

    act(() => window.dispatchEvent(new Event("afterprint")))
    const screenStyle = window.getComputedStyle(document.body)
    expect(document.body).toHaveAttribute("data-scroll-locked", "1")
    expect(screenStyle.overflow).toBe("hidden")
    expect(screenStyle.position).toBe("relative")
  })

  it("releases the Story table's actual scroll root and sticky header for print", () => {
    const { container } = render(<ChartFixture pageWidth={360} />)
    const tableRoot = document.createElement("div")
    tableRoot.setAttribute("data-block-type", "table")
    tableRoot.className = "max-h-96 overflow-auto"
    const table = document.createElement("table")
    const header = table.createTHead()
    header.className = "sticky"
    header.insertRow().insertCell().textContent = "Column"
    table.createTBody().insertRow().insertCell().textContent = "Last rendered row"
    tableRoot.append(table)
    container.querySelector("main > div")!.append(tableRoot)
    addStyle(".max-h-96 { max-height: 384px } .overflow-auto { overflow: auto } .sticky { position: sticky }")
    expect(window.getComputedStyle(tableRoot).maxHeight).toBe("384px")
    expect(window.getComputedStyle(header).position).toBe("sticky")

    fireEvent.click(screen.getByText("Export chart"))
    applyPrintDeclarations()

    expect(window.getComputedStyle(tableRoot).maxHeight).toBe("none")
    expect(window.getComputedStyle(tableRoot).overflow).toBe("visible")
    expect(window.getComputedStyle(header).position).toBe("static")
    expect(window.getComputedStyle(header).display).toBe("table-header-group")
    expect(window.getComputedStyle(table).tableLayout).toBe("fixed")
    expect(tableRoot).toHaveTextContent("Last rendered row")
  })
})
