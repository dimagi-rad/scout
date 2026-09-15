import { useCallback, useEffect, useRef } from "react"
import { useAppStore } from "@/store/store"

/** Guard a captured async callback before navigation or other external effects. */
export function useIsCurrentAccount() {
  const accountSession = useAppStore((state) => state.accountSession)
  const mounted = useRef(false)
  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false }
  }, [])
  return useCallback(() => mounted.current && accountSession.isCurrent(), [accountSession])
}
