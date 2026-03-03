import { createContext, useContext, useEffect, useRef, useState } from "react"

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
        const res = await fetch("/health/", { method: "GET", cache: "no-store" })
        if (res.ok) {
          if (wasOfflineRef.current) {
            wasOfflineRef.current = false
            setStatus("reconnecting")
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
        wasOfflineRef.current = true
        setStatus("offline")
      }
    }

    // Fast first check via navigator.onLine
    if (!navigator.onLine) {
      wasOfflineRef.current = true
      setStatus("offline")
    }

    checkHealth()
    const interval = setInterval(checkHealth, POLL_INTERVAL)

    const handleOnline = () => checkHealth()
    const handleOffline = () => {
      wasOfflineRef.current = true
      setStatus("offline")
    }

    window.addEventListener("online", handleOnline)
    window.addEventListener("offline", handleOffline)

    return () => {
      polling = false
      clearInterval(interval)
      if (reconnectTimerRef.current) clearTimeout(reconnectTimerRef.current)
      window.removeEventListener("online", handleOnline)
      window.removeEventListener("offline", handleOffline)
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
