import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { api, getCsrfToken } from "@/api/client"
import { postOAuthStart } from "./oauth"

vi.mock("@/api/client", () => ({ api: { get: vi.fn() }, getCsrfToken: vi.fn() }))

const HREF = "/accounts/ocs/login/?process=connect&next=%2Fconnections"

describe("postOAuthStart", () => {
  let submitted: HTMLFormElement[]

  beforeEach(() => {
    submitted = []
    vi.spyOn(HTMLFormElement.prototype, "submit").mockImplementation(function (
      this: HTMLFormElement,
    ) {
      submitted.push(this)
    })
  })

  afterEach(() => {
    vi.restoreAllMocks()
    document.body.innerHTML = ""
  })

  function fields(form: HTMLFormElement) {
    return Object.fromEntries(new FormData(form).entries())
  }

  it("posts the login URL's params with the CSRF token", async () => {
    vi.mocked(getCsrfToken).mockReturnValue("tok")

    await postOAuthStart(HREF)

    expect(submitted).toHaveLength(1)
    const [form] = submitted
    expect(form.method).toBe("post")
    expect(new URL(form.action).pathname).toBe("/accounts/ocs/login/")
    expect(new URL(form.action).search).toBe("")
    expect(fields(form)).toEqual({
      process: "connect",
      next: "/connections",
      csrfmiddlewaretoken: "tok",
    })
  })

  it("fetches a CSRF token first when the cookie is missing", async () => {
    vi.mocked(getCsrfToken).mockReturnValueOnce("").mockReturnValue("fresh")
    vi.mocked(api.get).mockResolvedValue({})

    await postOAuthStart(HREF)

    expect(api.get).toHaveBeenCalledWith("/api/auth/csrf/")
    expect(fields(submitted[0]).csrfmiddlewaretoken).toBe("fresh")
  })

  it("falls back to the GET confirmation page when no token can be had", async () => {
    vi.mocked(getCsrfToken).mockReturnValue("")
    vi.mocked(api.get).mockRejectedValue(new Error("offline"))
    const open = vi.spyOn(window, "open").mockReturnValue(null)

    await postOAuthStart(HREF)

    expect(submitted).toHaveLength(0)
    expect(open).toHaveBeenCalledWith(HREF, "_self")
  })
})
