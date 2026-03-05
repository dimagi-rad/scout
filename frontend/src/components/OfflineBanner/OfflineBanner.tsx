import { Wifi, WifiOff } from "lucide-react"
import { useNetworkStatus } from "@/hooks/useNetworkStatus"

export function OfflineBanner() {
  const { status } = useNetworkStatus()

  if (status === "online") return null

  return (
    <div
      className={`fixed bottom-0 left-0 right-0 z-50 flex items-center justify-center gap-2 px-4 py-3 text-sm font-medium transition-all duration-300 ${
        status === "reconnecting"
          ? "bg-green-600 text-white"
          : "bg-destructive text-destructive-foreground"
      }`}
      role="status"
      aria-live="polite"
      data-testid="offline-banner"
    >
      {status === "reconnecting" ? (
        <>
          <Wifi className="h-4 w-4" />
          <span>Reconnected</span>
        </>
      ) : (
        <>
          <WifiOff className="h-4 w-4 animate-pulse" />
          <span>Server unreachable — attempting to reconnect...</span>
        </>
      )}
    </div>
  )
}
