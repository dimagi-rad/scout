import { render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

import type { Recipe } from "@/store/recipeSlice"
import { RecipeRunner } from "./RecipeRunner"

const recipe: Recipe = {
  id: "recipe-1",
  name: "Daily visits",
  description: "",
  prompt: "Summarise visits on {{day}}",
  variables: [{ name: "day", type: "date", required: true }],
  is_shared: true,
  created_at: "2026-09-01T10:00:00Z",
  updated_at: "2026-09-01T10:00:00Z",
}

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllEnvs()
})

// CI runs in UTC, where a UTC-based formatter looks correct; pin zones on both sides.
describe.each(["Pacific/Kiritimati", "Asia/Kolkata", "UTC", "America/Los_Angeles", "Pacific/Pago_Pago"])(
  "RecipeRunner in %s",
  (tz) => {
    it.each([
      ["just after local midnight", 0, 30],
      ["just before local midnight", 23, 30],
    ])("defaults a date variable to the local day %s", (_label, hour, minute) => {
      vi.stubEnv("TZ", tz)
      vi.useFakeTimers({ toFake: ["Date"] })
      vi.setSystemTime(new Date(2025, 8, 22, hour, minute))

      render(
        <RecipeRunner
          open
          onOpenChange={vi.fn()}
          recipe={recipe}
          onRun={vi.fn()}
          onRunComplete={vi.fn()}
        />,
      )

      expect(screen.getByLabelText("day")).toHaveValue("2025-09-22")
    })
  },
)
