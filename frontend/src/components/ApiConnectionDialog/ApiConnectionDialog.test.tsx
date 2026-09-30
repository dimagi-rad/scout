import { fireEvent, render, screen, waitFor } from "@testing-library/react"
import { beforeEach, describe, expect, it, vi } from "vitest"

import { api } from "@/api/client"
import { ApiConnectionDialog } from "./ApiConnectionDialog"

vi.mock("@/api/client", () => ({ api: { get: vi.fn(), post: vi.fn(), patch: vi.fn() } }))

const commcareSchema = {
  id: "commcare",
  display_name: "CommCare HQ",
  fields: [
    {
      key: "server",
      label: "CommCare HQ server",
      type: "select",
      required: false,
      editable_on_rotate: false,
      options: [
        { value: "", label: "Global (www.commcarehq.org)" },
        { value: "eu", label: "EU (eu.commcarehq.org)" },
      ],
    },
    { key: "domain", label: "Domain", type: "text", required: true, editable_on_rotate: false },
    { key: "username", label: "Username", type: "text", required: true, editable_on_rotate: true },
    { key: "api_key", label: "API Key", type: "password", required: true, editable_on_rotate: true },
  ],
}

function renderDialog(mode: "add" | "edit" = "add") {
  const editing =
    mode === "edit"
      ? {
          connection_id: "c1",
          provider: "commcare",
          credential_type: "api_key",
          scope_key: "eu",
          scope_label: "EU",
          status: null,
          chatbots: [],
        }
      : null
  render(
    <ApiConnectionDialog open mode={mode} editing={editing} onClose={vi.fn()} onSaved={vi.fn()} />,
  )
}

describe("ApiConnectionDialog", () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(api.get).mockResolvedValue([commcareSchema])
    vi.mocked(api.post).mockResolvedValue({ memberships: [] })
  })

  it("lets a CommCare API key be added for the EU server", async () => {
    renderDialog()

    const server = await screen.findByTestId("api-connection-field-server")
    fireEvent.change(server, { target: { value: "eu" } })
    fireEvent.change(screen.getByTestId("api-connection-field-domain"), { target: { value: "dom" } })
    fireEvent.change(screen.getByTestId("api-connection-field-username"), {
      target: { value: "u@example.com" },
    })
    fireEvent.change(screen.getByTestId("api-connection-field-api_key"), { target: { value: "k" } })
    fireEvent.click(screen.getByTestId("api-connection-submit"))

    await waitFor(() =>
      expect(api.post).toHaveBeenCalledWith("/api/auth/connections/", {
        provider: "commcare",
        fields: { server: "eu", domain: "dom", username: "u@example.com", api_key: "k" },
      }),
    )
  })

  it("defaults the server to the first option (www)", async () => {
    renderDialog()

    const server = (await screen.findByTestId("api-connection-field-server")) as HTMLSelectElement
    expect(server.value).toBe("")
  })

  it("does not offer to change the server when rotating a key", async () => {
    renderDialog("edit")

    await screen.findByTestId("api-connection-field-api_key")
    expect(screen.queryByTestId("api-connection-field-server")).toBeNull()
  })
})
