import { useEffect } from "react"
import { createPortal } from "react-dom"
import { act, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { useArtifactPrint } from "./useArtifactPrint"

function PrintFixture({ id = "one", portal = false, showTarget = true }: {
  id?: string
  portal?: boolean
  showTarget?: boolean
}) {
  const { printRef, printArtifact, printError } = useArtifactPrint(id)
  const content = (
    <section data-testid={`viewer-${id}`}>
      <button onClick={printArtifact}>Export {id}</button>
      {printError && <p role="alert">{printError}</p>}
      {showTarget && (
        <div ref={printRef} data-testid={`target-${id}`} className="h-full overflow-y-auto">
          <h1>Selected artifact {id}</h1>
          <select aria-label={`Period ${id}`} defaultValue="previous-year">
            <option value="previous-period">Previous period</option>
            <option value="previous-year">Same period last year</option>
          </select>
          <svg viewBox="0 0 800 300" data-testid={`chart-${id}`}>
            <defs><clipPath id={`clip-${id}`}><rect width="800" height="300" /></clipPath></defs>
            <path d="M0,1 L4,8" clipPath={`url(#clip-${id})`} />
          </svg>
          <p>Final story block {id}</p>
        </div>
      )}
    </section>
  )
  return portal ? createPortal(content, document.body) : content
}

describe("selected artifact print lifecycle", () => {
  beforeEach(() => vi.spyOn(window, "print").mockImplementation(() => {}))
  afterEach(() => {
    window.dispatchEvent(new Event("afterprint"))
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
  })

  it.each([false, true])("marks only the selected ancestry, including portal=%s", (portal) => {
    const { container } = render(
      <main>
        <nav>Primary navigation</nav>
        <aside>Private chat content</aside>
        <PrintFixture portal={portal} />
        <PrintFixture id="other" />
      </main>,
    )
    const original = document.body.innerHTML
    const target = screen.getByTestId("target-one")
    const svg = screen.getByTestId("chart-one")
    const selectedPeriod = screen.getByLabelText("Period one")

    fireEvent.click(screen.getByRole("button", { name: "Export one" }))

    expect(window.print).toHaveBeenCalledTimes(1)
    expect(target).toHaveAttribute("data-scout-print", "target")
    expect(screen.getByTestId("viewer-one")).toHaveAttribute("data-scout-print", "ancestor")
    expect(document.body).toHaveAttribute("data-scout-print", "ancestor")
    expect(document.documentElement).toHaveAttribute("data-scout-print", "ancestor")
    expect(container.hasAttribute("data-scout-print")).toBe(!portal)
    expect(screen.getByRole("navigation")).not.toHaveAttribute("data-scout-print")
    expect(screen.getByTestId("target-other")).not.toHaveAttribute("data-scout-print")
    expect(screen.getByTestId("chart-one")).toBe(svg)
    expect(svg).toHaveAttribute("viewBox", "0 0 800 300")
    expect(svg.querySelector("path")).toHaveAttribute("clip-path", "url(#clip-one)")
    expect(screen.getByLabelText("Period one")).toBe(selectedPeriod)
    expect(selectedPeriod).toHaveValue("previous-year")
    expect(document.querySelectorAll("#clip-one")).toHaveLength(1)

    // afterprint is emitted for both a saved print and a cancelled preview.
    act(() => window.dispatchEvent(new Event("afterprint")))
    expect(document.body.innerHTML).toBe(original)
    expect(document.querySelector("[data-scout-print]")).toBeNull()
  })

  it("keeps the layout while a nonblocking print dialog is open, then allows a new export", () => {
    render(<PrintFixture />)
    fireEvent.click(screen.getByText("Export one"))
    fireEvent.click(screen.getByText("Export one"))
    expect(window.print).toHaveBeenCalledTimes(1)
    expect(screen.getByTestId("target-one")).toHaveAttribute("data-scout-print", "target")

    act(() => window.dispatchEvent(new Event("afterprint")))
    fireEvent.click(screen.getByText("Export one"))
    expect(window.print).toHaveBeenCalledTimes(2)
  })

  it("does not let another mounted artifact steal an active print session", () => {
    const { rerender } = render(<><PrintFixture /><PrintFixture id="other" /></>)
    fireEvent.click(screen.getByText("Export one"))
    fireEvent.click(screen.getByText("Export other"))
    expect(window.print).toHaveBeenCalledTimes(1)
    expect(screen.getByTestId("target-one")).toHaveAttribute("data-scout-print", "target")
    expect(screen.getByTestId("target-other")).not.toHaveAttribute("data-scout-print")
    rerender(<><PrintFixture /></>)
    expect(screen.getByTestId("target-one")).toHaveAttribute("data-scout-print", "target")
    act(() => window.dispatchEvent(new Event("afterprint")))
    expect(document.querySelector("[data-scout-print]")).toBeNull()
  })

  it("restores prior attributes exactly and does not change inline layout styles", () => {
    const { container } = render(<PrintFixture />)
    container.setAttribute("data-scout-print", "existing-value")
    container.setAttribute("style", "overflow: hidden; height: 700px")
    const target = screen.getByTestId("target-one")
    target.setAttribute("style", "overflow-y: auto; height: 500px")
    fireEvent.click(screen.getByText("Export one"))
    act(() => window.dispatchEvent(new Event("afterprint")))
    expect(container).toHaveAttribute("data-scout-print", "existing-value")
    expect(container).toHaveAttribute("style", "overflow: hidden; height: 700px")
    expect(target).toHaveAttribute("style", "overflow-y: auto; height: 500px")
    container.removeAttribute("data-scout-print")
  })

  it("also cleans up when the browser exits print media", () => {
    const media = new EventTarget()
    vi.stubGlobal("matchMedia", vi.fn(() => media))
    render(<PrintFixture />)
    const remove = vi.spyOn(media, "removeEventListener")
    fireEvent.click(screen.getByText("Export one"))
    const entering = Object.assign(new Event("change"), { matches: true })
    act(() => media.dispatchEvent(entering))
    expect(document.querySelector('[data-scout-print="target"]')).not.toBeNull()
    const leaving = Object.assign(new Event("change"), { matches: false })
    act(() => media.dispatchEvent(leaving))
    expect(document.querySelector("[data-scout-print]")).toBeNull()
    expect(remove).toHaveBeenCalledWith("change", expect.any(Function))
  })

  it.each(["unmount", "artifact change", "target removed", "pagehide"])("cleans up on %s", (reason) => {
    const remove = vi.spyOn(window, "removeEventListener")
    const view = render(<PrintFixture />)
    fireEvent.click(screen.getByText("Export one"))
    if (reason === "unmount") view.unmount()
    else if (reason === "artifact change") view.rerender(<PrintFixture id="changed" />)
    else if (reason === "target removed") view.rerender(<PrintFixture showTarget={false} />)
    else act(() => window.dispatchEvent(new Event("pagehide")))
    expect(document.querySelector("[data-scout-print]")).toBeNull()
    expect(remove).toHaveBeenCalledWith("afterprint", expect.any(Function))
    expect(remove).toHaveBeenCalledWith("pagehide", expect.any(Function))
  })

  it("cleans up a native print error and lets the user retry", () => {
    vi.mocked(window.print).mockImplementationOnce(() => { throw new Error("Unavailable") })
    render(<PrintFixture />)
    fireEvent.click(screen.getByText("Export one"))
    expect(document.querySelector("[data-scout-print]")).toBeNull()
    expect(screen.getByRole("alert")).toHaveTextContent("Could not open the print dialog. Please try again.")
    fireEvent.click(screen.getByText("Export one"))
    expect(window.print).toHaveBeenCalledTimes(2)
    expect(screen.queryByRole("alert")).toBeNull()
  })

  it("does not print the application when the artifact has not loaded", () => {
    render(<PrintFixture showTarget={false} />)
    fireEvent.click(screen.getByText("Export one"))
    expect(window.print).not.toHaveBeenCalled()
    expect(document.querySelector("[data-scout-print]")).toBeNull()
    expect(screen.getByRole("alert")).toHaveTextContent("The artifact is not ready to export")
  })

  it("keeps the rendered subtree mounted throughout printing and cancellation", () => {
    const onMount = vi.fn()
    const onUnmount = vi.fn()
    function RenderedContent() {
      useEffect(() => { onMount(); return onUnmount }, [])
      return <PrintFixture />
    }
    render(<RenderedContent />)
    fireEvent.click(screen.getByText("Export one"))
    act(() => window.dispatchEvent(new Event("afterprint")))
    expect(onMount).toHaveBeenCalledTimes(1)
    expect(onUnmount).not.toHaveBeenCalled()
  })
})
