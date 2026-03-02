import { useEffect, useCallback, useRef } from "react"
import { useEmbedParams } from "./useEmbedParams"

type MessageHandler = (type: string, payload: Record<string, unknown>) => void

/**
 * Resolve the trusted parent origin for postMessage validation.
 * Uses document.referrer (set when the iframe is loaded by the host page).
 */
function getParentOrigin(): string | null {
  try {
    if (document.referrer) {
      return new URL(document.referrer).origin
    }
  } catch {
    // malformed referrer
  }
  return null
}

export function useEmbedMessaging(onCommand?: MessageHandler) {
  const { isEmbed } = useEmbedParams()
  const parentOrigin = useRef(getParentOrigin())

  const sendEvent = useCallback(
    (type: string, payload?: Record<string, unknown>) => {
      if (!isEmbed || window.parent === window) return
      const origin = parentOrigin.current
      if (!origin) return
      window.parent.postMessage({ type, ...payload }, origin)
    },
    [isEmbed]
  )

  useEffect(() => {
    if (!isEmbed || !onCommand) return
    const trustedOrigin = parentOrigin.current

    function handleMessage(event: MessageEvent) {
      if (!trustedOrigin || event.origin !== trustedOrigin) return
      const data = event.data
      if (!data || typeof data.type !== "string" || !data.type.startsWith("scout:")) return
      onCommand?.(data.type, data.payload || {})
    }

    window.addEventListener("message", handleMessage)
    return () => window.removeEventListener("message", handleMessage)
  }, [isEmbed, onCommand])

  return { sendEvent }
}
