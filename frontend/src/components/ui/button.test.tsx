import { render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { expect, it, vi } from "vitest"
import { Button } from "./button"

it("requires explicit submit intent inside a form", async () => {
  const submit = vi.fn((event) => event.preventDefault())
  const user = userEvent.setup()
  render(<form onSubmit={submit}><Button>Filter</Button><Button type="submit">Save</Button></form>)
  await user.click(screen.getByRole("button", { name: "Filter" }))
  expect(submit).not.toHaveBeenCalled()
  await user.click(screen.getByRole("button", { name: "Save" }))
  expect(submit).toHaveBeenCalledOnce()
})

it("preserves the child element when rendering asChild", () => {
  render(<Button asChild><a href="/workspaces">Workspaces</a></Button>)
  expect(screen.getByRole("link")).not.toHaveAttribute("type")
})
