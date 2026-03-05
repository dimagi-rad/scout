import { useEffect, useRef } from "react"
import { useEmbedParams } from "./useEmbedParams"
import { useEmbedMessaging } from "./useEmbedMessaging"

/**
 * Observes document.documentElement and sends `scout:resize` to the parent
 * frame whenever scrollHeight changes. Uses requestAnimationFrame for
 * debouncing. Only active when running inside an iframe embed.
 */
export function useAutoResize() {
  const { isEmbed } = useEmbedParams()
  const { sendEvent } = useEmbedMessaging()
  const lastHeight = useRef(0)
  const rafId = useRef(0)

  useEffect(() => {
    if (!isEmbed || window.parent === window) return

    function reportHeight() {
      const height = document.documentElement.scrollHeight
      if (height !== lastHeight.current) {
        lastHeight.current = height
        sendEvent("scout:resize", { height })
      }
    }

    // Report initial height
    reportHeight()

    const observer = new ResizeObserver(() => {
      cancelAnimationFrame(rafId.current)
      rafId.current = requestAnimationFrame(reportHeight)
    })

    observer.observe(document.documentElement)

    return () => {
      observer.disconnect()
      cancelAnimationFrame(rafId.current)
    }
  }, [isEmbed, sendEvent])
}
