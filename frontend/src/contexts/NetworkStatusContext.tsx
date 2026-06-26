import { createContext, useContext, useEffect, useRef, useState } from "react"
import { withBasePath } from "@/config"

export type NetworkStatus = "online" | "offline" | "reconnecting"

interface NetworkStatusContextValue {
  isOnline: boolean
  status: NetworkStatus
}

const NetworkStatusContext = createContext<NetworkStatusContextValue>({
  isOnline: true,
  status: "online",
})

const POLL_INTERVAL = 5000
const RECONNECT_DISPLAY_MS = 2000

export function NetworkStatusProvider({ children }: { children: React.ReactNode }) {
  const [status, setStatus] = useState<NetworkStatus>("online")
  const wasOfflineRef = useRef(false)
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  useEffect(() => {
    let polling = true

    async function checkHealth() {
      if (!polling) return
      try {
        const res = await fetch(withBasePath("/health/"), { method: "GET", cache: "no-store" })
        if (res.ok) {
          if (wasOfflineRef.current) {
            wasOfflineRef.current = false
            setStatus("reconnecting")
            if (reconnectTimerRef.current) clearTimeout(reconnectTimerRef.current)
            reconnectTimerRef.current = setTimeout(() => {
              setStatus("online")
            }, RECONNECT_DISPLAY_MS)
          } else {
            setStatus("online")
          }
        } else {
          throw new Error("not ok")
        }
      } catch {
        if (reconnectTimerRef.current) {
          clearTimeout(reconnectTimerRef.current)
          reconnectTimerRef.current = null
        }
        wasOfflineRef.current = true
        setStatus("offline")
      }
    }

    // Fast first check via navigator.onLine
    if (!navigator.onLine) {
      wasOfflineRef.current = true
      setStatus("offline")
    }

    let interval: ReturnType<typeof setInterval> | null = null
    const startPolling = () => {
      if (interval !== null) return
      interval = setInterval(checkHealth, POLL_INTERVAL)
    }
    const stopPolling = () => {
      if (interval !== null) {
        clearInterval(interval)
        interval = null
      }
    }

    // Gate the /health/ poll on tab visibility (arch #254, 05#6): a hidden tab
    // doesn't need a live network indicator. Re-check immediately on show.
    const handleVisibility = () => {
      if (document.visibilityState === "visible") {
        checkHealth()
        startPolling()
      } else {
        stopPolling()
      }
    }

    checkHealth()
    if (document.visibilityState === "visible") startPolling()

    const handleOnline = () => checkHealth()
    const handleOffline = () => {
      if (reconnectTimerRef.current) {
        clearTimeout(reconnectTimerRef.current)
        reconnectTimerRef.current = null
      }
      wasOfflineRef.current = true
      setStatus("offline")
    }

    window.addEventListener("online", handleOnline)
    window.addEventListener("offline", handleOffline)
    document.addEventListener("visibilitychange", handleVisibility)

    return () => {
      polling = false
      stopPolling()
      if (reconnectTimerRef.current) clearTimeout(reconnectTimerRef.current)
      window.removeEventListener("online", handleOnline)
      window.removeEventListener("offline", handleOffline)
      document.removeEventListener("visibilitychange", handleVisibility)
    }
  }, [])

  return (
    <NetworkStatusContext.Provider value={{ isOnline: status === "online" || status === "reconnecting", status }}>
      {children}
    </NetworkStatusContext.Provider>
  )
}

// eslint-disable-next-line react-refresh/only-export-components
export function useNetworkStatus() {
  return useContext(NetworkStatusContext)
}
