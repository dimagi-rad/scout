import { describe, expect, it } from "vitest"
import { render, screen } from "@testing-library/react"

describe("vitest runner", () => {
  it("renders into jsdom and exposes jest-dom matchers", () => {
    render(<div data-testid="probe">ready</div>)
    expect(screen.getByTestId("probe")).toBeInTheDocument()
    expect(screen.getByTestId("probe")).toHaveTextContent("ready")
  })

  it("exposes crypto.randomUUID (used by the store)", () => {
    expect(typeof crypto.randomUUID()).toBe("string")
  })
})
