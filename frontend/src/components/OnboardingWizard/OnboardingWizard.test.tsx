import { fireEvent, render, screen, waitFor } from "@testing-library/react"
import { describe, expect, it, vi } from "vitest"

import { api } from "@/api/client"
import { OnboardingWizard } from "./OnboardingWizard"

vi.mock("@/api/client", () => ({ api: { get: vi.fn(), post: vi.fn() } }))

function providers(status: string | null) {
  return {
    providers: [
      { id: "ocs", name: "Open Chat Studio", login_url: "/accounts/ocs/login/", status },
    ],
  }
}

describe("OnboardingWizard", () => {
  it("lets a user whose OCS sign-in has no team connect a team", async () => {
    vi.mocked(api.get).mockResolvedValue(providers("needs_team"))
    render(<OnboardingWizard />)

    const link = await screen.findByTestId("onboarding-ocs")
    expect(link.getAttribute("href")).toBe("/accounts/ocs/login/?process=connect&next=%2F")
    expect(screen.getByTestId("onboarding-ocs-needs-team")).toBeTruthy()
  })

  it("offers OCS without the team warning when nothing is wrong", async () => {
    vi.mocked(api.get).mockResolvedValue(providers(null))
    render(<OnboardingWizard />)

    expect(await screen.findByTestId("onboarding-ocs")).toBeTruthy()
    expect(screen.queryByTestId("onboarding-ocs-needs-team")).toBeNull()
  })

  it("offers EU CommCare HQ sign-in once it is configured", async () => {
    vi.mocked(api.get).mockResolvedValue({
      providers: [
        {
          id: "commcare_eu",
          name: "CommCare HQ (EU)",
          login_url: "/accounts/commcare_eu/login/",
          status: null,
        },
      ],
    })
    render(<OnboardingWizard />)

    const link = await screen.findByTestId("onboarding-oauth-commcare-eu")
    expect(link.getAttribute("href")).toBe(
      "/accounts/commcare_eu/login/?process=connect&next=%2F",
    )
    expect(link.textContent).toBe("Connect with CommCare HQ (EU)")
  })

  it("drops the www sign-in on a deployment that configured only EU", async () => {
    vi.mocked(api.get).mockResolvedValue({
      providers: [
        { id: "commcare_eu", name: "CommCare HQ (EU)", login_url: "/accounts/commcare_eu/login/" },
      ],
    })
    render(<OnboardingWizard />)

    await screen.findByTestId("onboarding-oauth-commcare-eu")
    expect(screen.queryByTestId("onboarding-oauth")).toBeNull()
  })

  it("hides EU CommCare HQ sign-in when it is not configured", async () => {
    vi.mocked(api.get).mockResolvedValue({
      providers: [
        { id: "commcare", name: "CommCare HQ", login_url: "/accounts/commcare/login/" },
        ...providers(null).providers,
      ],
    })
    render(<OnboardingWizard />)

    await screen.findByTestId("onboarding-ocs")
    expect(screen.queryByTestId("onboarding-oauth-commcare-eu")).toBeNull()
    const www = screen.getByTestId("onboarding-oauth")
    expect(www.textContent).toBe("Connect with OAuth")
    expect(www.getAttribute("href")).toBe("/accounts/commcare/login/?process=connect&next=%2F")
  })

  it("drops the www sign-in on an OCS-only deployment", async () => {
    vi.mocked(api.get).mockResolvedValue(providers(null))
    render(<OnboardingWizard />)

    await screen.findByTestId("onboarding-ocs")
    expect(screen.queryByTestId("onboarding-oauth")).toBeNull()
  })

  it("sends an API key to the CommCare server the user picks", async () => {
    vi.mocked(api.get).mockImplementation((path) =>
      Promise.resolve(
        path === "/api/auth/api-key-providers/"
          ? [
              {
                id: "commcare",
                fields: [
                  {
                    key: "server",
                    options: [
                      { value: "", label: "Global (www.commcarehq.org)" },
                      { value: "eu", label: "EU (eu.commcarehq.org)" },
                    ],
                  },
                ],
              },
            ]
          : providers(null),
      ),
    )
    vi.mocked(api.post).mockResolvedValue({ memberships: [] })
    render(<OnboardingWizard />)

    fireEvent.click(await screen.findByTestId("onboarding-api-key-option"))
    fireEvent.change(await screen.findByTestId("onboarding-server"), { target: { value: "eu" } })
    fireEvent.change(screen.getByTestId("onboarding-domain"), { target: { value: "dom" } })
    fireEvent.change(screen.getByTestId("onboarding-username"), {
      target: { value: "u@example.com" },
    })
    fireEvent.change(screen.getByTestId("onboarding-api-key"), { target: { value: "k" } })
    fireEvent.submit(screen.getByTestId("onboarding-domain").closest("form")!)

    await waitFor(() =>
      expect(api.post).toHaveBeenCalledWith("/api/auth/connections/", {
        provider: "commcare",
        fields: { server: "eu", domain: "dom", username: "u@example.com", api_key: "k" },
      }),
    )
  })

  it("says when sign-in options failed to load and retries", async () => {
    vi.mocked(api.get).mockRejectedValueOnce(new Error("503"))
    vi.mocked(api.get).mockResolvedValueOnce(providers(null))
    render(<OnboardingWizard />)

    const failure = await screen.findByTestId("onboarding-providers-error")
    fireEvent.click(failure.querySelector("button")!)

    expect(await screen.findByTestId("onboarding-ocs")).toBeTruthy()
    expect(screen.queryByTestId("onboarding-providers-error")).toBeNull()
  })
})
