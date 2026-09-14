import { useCallback, useEffect, useRef, useState } from "react"

import { api } from "@/api/client"
import type { ArtifactDataRecoveryState } from "./types"

const POLL_INTERVAL_MS = 2_000

export function useArtifactDataRecovery(
  artifactId: string,
  workspaceId: string,
  enabled: boolean,
) {
  const [state, setState] = useState<ArtifactDataRecoveryState | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [isStarting, setIsStarting] = useState(false)
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  const endpoint = `/api/workspaces/${workspaceId}/artifacts/${artifactId}/recovery/`

  const clearPoll = useCallback(() => {
    if (timerRef.current) {
      clearTimeout(timerRef.current)
      timerRef.current = null
    }
  }, [])

  const refetch = useCallback(async () => {
    if (!enabled) return null
    clearPoll()
    setError(null)
    try {
      const next = await api.get<ArtifactDataRecoveryState>(endpoint)
      setState(next)
      return next
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not check artifact data")
      return null
    }
  }, [clearPoll, enabled, endpoint])

  const startRecovery = useCallback(async () => {
    if (!enabled || isStarting) return
    clearPoll()
    setIsStarting(true)
    setError(null)
    try {
      const next = await api.post<ArtifactDataRecoveryState>(endpoint, {})
      setState(next)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not start data recovery")
    } finally {
      setIsStarting(false)
    }
  }, [clearPoll, enabled, endpoint, isStarting])

  useEffect(() => {
    clearPoll()
    setState(null)
    setError(null)
    if (!enabled) return
    void refetch()
    return clearPoll
  }, [clearPoll, enabled, refetch])

  useEffect(() => {
    clearPoll()
    if (!enabled || state?.status !== "recovering") return
    timerRef.current = setTimeout(() => void refetch(), POLL_INTERVAL_MS)
    return clearPoll
  }, [clearPoll, enabled, refetch, state?.status, state?.recovery?.progress])

  return {
    state,
    error,
    isChecking: enabled && !state && !error,
    isStarting,
    refetch,
    startRecovery,
  }
}
