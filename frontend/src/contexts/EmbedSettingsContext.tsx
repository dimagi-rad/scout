import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useState,
  type ReactNode,
} from "react"
import type { EmbedMode, EmbedTheme } from "@/hooks/useEmbedParams"

interface EmbedSettingsContextValue {
  mode: EmbedMode
  theme: EmbedTheme
  setMode: (mode: EmbedMode) => void
  setTheme: (theme: EmbedTheme) => void
}

const EmbedSettingsContext = createContext<EmbedSettingsContextValue | null>(null)

/**
 * Apply an embed theme by toggling the Tailwind `.dark` class on <html>. "auto"
 * follows the OS color-scheme preference. (issue #248, 06#6: theme was parsed
 * but never applied.)
 */
// eslint-disable-next-line react-refresh/only-export-components
export function applyTheme(theme: EmbedTheme): void {
  const prefersDark =
    theme === "dark" ||
    (theme === "auto" &&
      typeof window !== "undefined" &&
      window.matchMedia?.("(prefers-color-scheme: dark)").matches)
  document.documentElement.classList.toggle("dark", Boolean(prefersDark))
}

/**
 * Holds the *live* embed mode and theme so the widget's runtime `scout:set-mode`
 * command (and theme switches) take effect, instead of being frozen at the URL
 * value first read by a memoized hook.
 */
export function EmbedSettingsProvider({
  initialMode,
  initialTheme,
  children,
}: {
  initialMode: EmbedMode
  initialTheme: EmbedTheme
  children: ReactNode
}) {
  const [mode, setMode] = useState<EmbedMode>(initialMode)
  const [theme, setThemeState] = useState<EmbedTheme>(initialTheme)

  // Apply the current theme, and re-apply on OS preference changes while in
  // "auto" mode.
  useEffect(() => {
    applyTheme(theme)
    if (theme !== "auto" || typeof window === "undefined" || !window.matchMedia) {
      return
    }
    const mql = window.matchMedia("(prefers-color-scheme: dark)")
    const onChange = () => applyTheme("auto")
    mql.addEventListener("change", onChange)
    return () => mql.removeEventListener("change", onChange)
  }, [theme])

  const setTheme = useCallback((next: EmbedTheme) => setThemeState(next), [])

  return (
    <EmbedSettingsContext.Provider value={{ mode, theme, setMode, setTheme }}>
      {children}
    </EmbedSettingsContext.Provider>
  )
}

/**
 * Read the live embed settings. Falls back to sensible defaults when used
 * outside an EmbedSettingsProvider (e.g. the standalone, non-embedded app) so
 * shared components don't need to special-case the provider's presence.
 */
// eslint-disable-next-line react-refresh/only-export-components
export function useEmbedSettings(): EmbedSettingsContextValue {
  const ctx = useContext(EmbedSettingsContext)
  if (ctx) return ctx
  return {
    mode: "chat",
    theme: "auto",
    setMode: () => {},
    setTheme: () => {},
  }
}
