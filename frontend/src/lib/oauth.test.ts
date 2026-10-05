import { fireEvent } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { api, getCsrfToken } from "@/api/client"
import { postOAuthStart, startOAuthOnClick } from "./oauth"

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

  it("never posts the CSRF token to another origin", async () => {
    vi.mocked(getCsrfToken).mockReturnValue("tok")
    const open = vi.spyOn(window, "open").mockReturnValue(null)

    await postOAuthStart("https://evil.test/accounts/ocs/login/")

    expect(submitted).toHaveLength(0)
    expect(open).toHaveBeenCalledWith("https://evil.test/accounts/ocs/login/", "_self")
  })

  it("removes the form once submitted", async () => {
    vi.mocked(getCsrfToken).mockReturnValue("tok")

    await postOAuthStart(HREF)

    expect(submitted).toHaveLength(1)
    expect(document.querySelector("form")).toBeNull()
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

describe("startOAuthOnClick", () => {
  let submit: ReturnType<typeof vi.spyOn>
  let link: HTMLAnchorElement

  beforeEach(() => {
    vi.mocked(getCsrfToken).mockReturnValue("tok")
    submit = vi.spyOn(HTMLFormElement.prototype, "submit").mockImplementation(() => {})
    link = document.createElement("a")
    link.href = HREF
    link.addEventListener("click", (e) =>
      startOAuthOnClick(e as unknown as Parameters<typeof startOAuthOnClick>[0]),
    )
    document.body.appendChild(link)
  })

  afterEach(() => {
    vi.restoreAllMocks()
    document.body.innerHTML = ""
  })

  it("posts on a plain click", async () => {
    const notCancelled = fireEvent.click(link)

    expect(notCancelled).toBe(false)
    await vi.waitFor(() => expect(submit).toHaveBeenCalledTimes(1))
  })

  it.each([
    ["ctrl", { ctrlKey: true }],
    ["meta", { metaKey: true }],
    ["shift", { shiftKey: true }],
    ["alt", { altKey: true }],
    ["middle button", { button: 1 }],
  ])("leaves a %s click to the browser's GET", (_name, init) => {
    const notCancelled = fireEvent.click(link, init)

    expect(notCancelled).toBe(true)
    expect(submit).not.toHaveBeenCalled()
  })

  it("falls back to the GET when the POST can't be built", async () => {
    vi.mocked(getCsrfToken).mockReturnValue("tok")
    submit.mockImplementation(() => {
      throw new Error("blocked")
    })
    const open = vi.spyOn(window, "open").mockReturnValue(null)

    fireEvent.click(link)

    await vi.waitFor(() => expect(open).toHaveBeenCalledWith(link.href, "_self"))
  })

  it("ignores repeat clicks while a token is being fetched", async () => {
    vi.mocked(api.get).mockClear()
    vi.mocked(getCsrfToken).mockReturnValueOnce("").mockReturnValue("fresh")
    let resolve: (value: unknown) => void = () => {}
    vi.mocked(api.get).mockReturnValue(new Promise((r) => (resolve = r)))

    fireEvent.click(link)
    fireEvent.click(link)
    resolve({})

    await vi.waitFor(() => expect(submit).toHaveBeenCalledTimes(1))
    expect(api.get).toHaveBeenCalledTimes(1)
  })
})
