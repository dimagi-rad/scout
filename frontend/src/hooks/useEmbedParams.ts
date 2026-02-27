import { useMemo } from "react"

export type EmbedMode = "chat" | "chat+artifacts" | "full"
export type EmbedTheme = "light" | "dark" | "auto"

export interface EmbedParams {
  mode: EmbedMode
  tenant: string | null
  theme: EmbedTheme
  isEmbed: boolean
}

export function useEmbedParams(): EmbedParams {
  return useMemo(() => {
    const params = new URLSearchParams(window.location.search)
    const isEmbed = window.location.pathname.startsWith("/embed")
    return {
      mode: (params.get("mode") as EmbedMode) || "chat",
      tenant: params.get("tenant"),
      theme: (params.get("theme") as EmbedTheme) || "auto",
      isEmbed,
    }
  }, [])
}
