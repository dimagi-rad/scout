import { fireEvent, render, screen } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { api } from "@/api/client"
import { useEmbedParams } from "@/hooks/useEmbedParams"
import { LoginForm } from "./LoginForm"

vi.mock("@/api/client", () => ({ api: { get: vi.fn() }, getCsrfToken: () => "tok" }))
vi.mock("@/hooks/useEmbedParams", () => ({ useEmbedParams: vi.fn() }))

describe("LoginForm OAuth buttons", () => {
  let submit: ReturnType<typeof vi.spyOn>

  beforeEach(() => {
    submit = vi.spyOn(HTMLFormElement.prototype, "submit").mockImplementation(() => {})
    vi.mocked(api.get).mockResolvedValue({
      providers: [{ id: "commcare", name: "CommCare HQ", login_url: "/accounts/commcare/login/" }],
    })
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  function embed(isEmbed: boolean) {
    vi.mocked(useEmbedParams).mockReturnValue({
      mode: "chat",
      tenant: null,
      provider: "commcare_connect",
      theme: "auto",
      isEmbed,
    })
  }

  it("signs in with a CSRF-token POST", async () => {
    embed(false)
    render(<LoginForm />)

    fireEvent.click(await screen.findByTestId("oauth-login-commcare"))

    expect(submit).toHaveBeenCalledTimes(1)
  })

  it("keeps the GET link in an embed, whose CSRF cookie may be partitioned", async () => {
    embed(true)
    render(<LoginForm />)

    const link = await screen.findByTestId("oauth-login-commcare")
    fireEvent.click(link)

    expect(submit).not.toHaveBeenCalled()
    expect(link).toHaveAttribute("target", "_top")
  })
})
