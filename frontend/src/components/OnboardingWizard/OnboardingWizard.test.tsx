import { fireEvent, render, screen } from "@testing-library/react"
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
