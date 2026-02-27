import { useEffect, useCallback } from "react"
import { useEmbedParams } from "./useEmbedParams"

type MessageHandler = (type: string, payload: Record<string, unknown>) => void

export function useEmbedMessaging(onCommand?: MessageHandler) {
  const { isEmbed } = useEmbedParams()

  const sendEvent = useCallback(
    (type: string, payload?: Record<string, unknown>) => {
      if (!isEmbed || window.parent === window) return
      window.parent.postMessage({ type, ...payload }, "*")
    },
    [isEmbed]
  )

  useEffect(() => {
    if (!isEmbed || !onCommand) return

    function handleMessage(event: MessageEvent) {
      const data = event.data
      if (!data || typeof data.type !== "string" || !data.type.startsWith("scout:")) return
      onCommand?.(data.type, data.payload || {})
    }

    window.addEventListener("message", handleMessage)
    return () => window.removeEventListener("message", handleMessage)
  }, [isEmbed, onCommand])

  return { sendEvent }
}
