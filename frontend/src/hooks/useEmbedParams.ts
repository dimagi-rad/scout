import { useMemo } from "react"
import { BASE_PATH } from "@/config"

export type EmbedMode = "chat" | "chat+artifacts" | "full"
export type EmbedTheme = "light" | "dark" | "auto"

export interface EmbedParams {
  mode: EmbedMode
  tenant: string | null
  provider: string
  theme: EmbedTheme
  isEmbed: boolean
}

export function useEmbedParams(): EmbedParams {
  return useMemo(() => {
    const params = new URLSearchParams(window.location.search)
    const p = window.location.pathname
    const relPath = BASE_PATH && p.startsWith(BASE_PATH) ? p.slice(BASE_PATH.length) : p
    const isEmbed = relPath.startsWith("/embed")
    return {
      mode: (params.get("mode") as EmbedMode) || "chat",
      tenant: params.get("tenant"),
      provider: params.get("provider") || "commcare_connect",
      theme: (params.get("theme") as EmbedTheme) || "auto",
      isEmbed,
    }
  }, [])
}
