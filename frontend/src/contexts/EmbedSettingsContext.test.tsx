import { afterEach, beforeEach, describe, expect, it } from "vitest"
import { act, render, renderHook, screen } from "@testing-library/react"
import {
  EmbedSettingsProvider,
  applyTheme,
  useEmbedSettings,
} from "@/contexts/EmbedSettingsContext"

// Issue #248, finding 06#6: the embed widget's setMode()/theme were no-ops —
// mode was read once from a memoized hook and the set-mode handler only
// console.logged; theme was parsed but never applied. These tests pin live,
// settable mode + applied theme.

describe("applyTheme", () => {
  beforeEach(() => {
    document.documentElement.classList.remove("dark")
  })

  it("adds the dark class for theme=dark", () => {
    applyTheme("dark")
    expect(document.documentElement.classList.contains("dark")).toBe(true)
  })

  it("removes the dark class for theme=light", () => {
    document.documentElement.classList.add("dark")
    applyTheme("light")
    expect(document.documentElement.classList.contains("dark")).toBe(false)
  })

  it("follows the OS preference for theme=auto", () => {
    // jsdom's matchMedia reports no match by default → light.
    document.documentElement.classList.add("dark")
    applyTheme("auto")
    expect(document.documentElement.classList.contains("dark")).toBe(false)
  })
})

describe("EmbedSettingsProvider", () => {
  afterEach(() => {
    document.documentElement.classList.remove("dark")
  })

  it("seeds mode and theme from the initial values and applies the theme", () => {
    render(
      <EmbedSettingsProvider initialMode="full" initialTheme="dark">
        <Probe />
      </EmbedSettingsProvider>,
    )
    expect(screen.getByTestId("mode").textContent).toBe("full")
    expect(document.documentElement.classList.contains("dark")).toBe(true)
  })

  it("updates mode at runtime via setMode (no-op fix)", () => {
    const { result } = renderHook(() => useEmbedSettings(), {
      wrapper: ({ children }) => (
        <EmbedSettingsProvider initialMode="chat" initialTheme="auto">
          {children}
        </EmbedSettingsProvider>
      ),
    })
    expect(result.current.mode).toBe("chat")
    act(() => result.current.setMode("chat+artifacts"))
    expect(result.current.mode).toBe("chat+artifacts")
  })

  it("applies a theme change at runtime via setTheme", () => {
    const { result } = renderHook(() => useEmbedSettings(), {
      wrapper: ({ children }) => (
        <EmbedSettingsProvider initialMode="chat" initialTheme="light">
          {children}
        </EmbedSettingsProvider>
      ),
    })
    expect(document.documentElement.classList.contains("dark")).toBe(false)
    act(() => result.current.setTheme("dark"))
    expect(document.documentElement.classList.contains("dark")).toBe(true)
  })
})

function Probe() {
  const { mode } = useEmbedSettings()
  return <span data-testid="mode">{mode}</span>
}
