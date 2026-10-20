import { fireEvent, render, screen } from "@testing-library/react"
import { beforeEach, expect, it, vi } from "vitest"
import type { UserTenant } from "@/api/auth"
import { getUserTenantsCached } from "@/api/userTenantsCache"
import { useAppStore } from "@/store/store"
import { CombineSourcesPanel } from "./CombineSourcesPanel"

vi.mock("@/api/userTenantsCache", () => ({ getUserTenantsCached: vi.fn() }))

function source(id: string, name: string, provider = "commcare_connect"): UserTenant {
  return {
    id: `m-${id}`,
    tenant_uuid: id,
    provider,
    tenant_id: id,
    tenant_name: name,
    last_selected_at: null,
  }
}

const onCombine = vi.fn()

async function renderPanel(sources: UserTenant[]) {
  vi.mocked(getUserTenantsCached).mockResolvedValue(sources)
  render(<CombineSourcesPanel onBack={vi.fn()} onCombine={onCombine} />)
  await screen.findByText(/Search \d+ data sources/)
}

function search(value: string) {
  fireEvent.change(screen.getByTestId("workspace-basket-search"), { target: { value } })
}

beforeEach(() => {
  vi.clearAllMocks()
  useAppStore.setState(useAppStore.getInitialState(), true)
  useAppStore.setState({ user: { id: "user-1" } as never })
})

it("lists nothing until the user types", async () => {
  await renderPanel([source("a", "Alpha"), source("b", "Beta")])

  expect(screen.queryByTestId("workspace-basket-option-a")).toBeNull()
  search("alp")
  expect(screen.getByTestId("workspace-basket-option-a")).toBeInTheDocument()
  expect(screen.queryByTestId("workspace-basket-option-b")).toBeNull()
})

it("keeps ticks across searches and pins them as chips", async () => {
  await renderPanel([source("a", "Alpha"), source("b", "Beta")])

  search("alp")
  fireEvent.click(screen.getByTestId("workspace-basket-option-a"))
  search("bet")
  fireEvent.click(screen.getByTestId("workspace-basket-option-b"))
  search("alp")

  expect(screen.getByTestId("workspace-basket-option-a")).toBeChecked()
  expect(screen.getByTestId("workspace-basket-chip-a")).toHaveTextContent("Alpha")
  expect(screen.getByTestId("workspace-basket-chip-b")).toHaveTextContent("Beta")
})

it("counts up on the combine button and needs two sources", async () => {
  await renderPanel([source("a", "Alpha 1"), source("b", "Alpha 2"), source("c", "Alpha 3")])
  const button = screen.getByTestId("workspace-basket-combine")
  search("alpha")

  expect(button).toBeDisabled()
  fireEvent.click(screen.getByTestId("workspace-basket-option-a"))
  expect(button).toBeDisabled()
  fireEvent.click(screen.getByTestId("workspace-basket-option-b"))
  expect(button).toHaveTextContent("Combine 2")
  fireEvent.click(screen.getByTestId("workspace-basket-option-c"))
  expect(button).toHaveTextContent("Combine 3")

  fireEvent.click(button)
  expect(onCombine).toHaveBeenCalledWith("Alpha (3 sources)", ["a", "b", "c"])
})

it("removes a chip, and clears the whole basket", async () => {
  await renderPanel([source("a", "Alpha"), source("b", "Beta")])
  search("a")
  fireEvent.click(screen.getByTestId("workspace-basket-option-a"))
  fireEvent.click(screen.getByTestId("workspace-basket-option-b"))

  fireEvent.click(screen.getByRole("button", { name: "Remove Alpha" }))
  expect(screen.queryByTestId("workspace-basket-chip-a")).toBeNull()
  expect(screen.getByTestId("workspace-basket-option-a")).not.toBeChecked()

  fireEvent.click(screen.getByTestId("workspace-basket-clear"))
  expect(screen.queryByTestId("workspace-basket-chip-b")).toBeNull()
  expect(screen.getByTestId("workspace-basket-clear")).toBeDisabled()
})

it("caps results and asks for a narrower search", async () => {
  const many = Array.from({ length: 60 }, (_, i) =>
    source(`s${i}`, `Opp ${String(i).padStart(2, "0")}`),
  )
  await renderPanel(many)
  search("opp")

  expect(screen.getAllByTestId(/^workspace-basket-option-/)).toHaveLength(50)
  expect(screen.getByTestId("workspace-basket-more")).toHaveTextContent(
    "10 more — refine your search",
  )
})

it("warns, without blocking, above 25 Connect opportunities", async () => {
  const many = Array.from({ length: 26 }, (_, i) =>
    source(`s${i}`, `Opp ${String(i).padStart(2, "0")}`),
  )
  await renderPanel(many)
  search("opp")
  for (const s of many.slice(0, 25)) {
    fireEvent.click(screen.getByTestId(`workspace-basket-option-${s.tenant_uuid}`))
  }
  expect(screen.queryByTestId("workspace-basket-connect-warning")).toBeNull()

  fireEvent.click(screen.getByTestId("workspace-basket-option-s25"))
  expect(screen.getByTestId("workspace-basket-connect-warning")).toHaveTextContent("26")
  expect(screen.getByTestId("workspace-basket-combine")).toBeEnabled()
})
