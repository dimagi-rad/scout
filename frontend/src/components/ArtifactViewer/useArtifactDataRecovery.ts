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
  const [pollAttempt, setPollAttempt] = useState(0)
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const scopeRef = useRef(0)

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
    const scope = scopeRef.current
    setError(null)
    try {
      const next = await api.get<ArtifactDataRecoveryState>(endpoint)
      if (scope !== scopeRef.current) return null
      setState(next)
      return next
    } catch (cause) {
      if (scope !== scopeRef.current) return null
      setError(cause instanceof Error ? cause.message : "Could not check artifact data")
      return null
    } finally {
      if (scope === scopeRef.current) setPollAttempt(attempt => attempt + 1)
    }
  }, [clearPoll, enabled, endpoint])

  const startRecovery = useCallback(async () => {
    if (!enabled || isStarting) return
    clearPoll()
    const scope = ++scopeRef.current
    setIsStarting(true)
    setError(null)
    try {
      const next = await api.post<ArtifactDataRecoveryState>(endpoint, {})
      if (scope !== scopeRef.current) return
      setState(next)
    } catch (cause) {
      if (scope !== scopeRef.current) return
      setError(cause instanceof Error ? cause.message : "Could not start data recovery")
    } finally {
      if (scope === scopeRef.current) {
        setIsStarting(false)
        setPollAttempt(attempt => attempt + 1)
      }
    }
  }, [clearPoll, enabled, endpoint, isStarting])

  useEffect(() => {
    clearPoll()
    scopeRef.current += 1
    setState(null)
    setError(null)
    setIsStarting(false)
    if (!enabled) return
    void refetch()
    return () => {
      scopeRef.current += 1
      clearPoll()
    }
  }, [clearPoll, enabled, refetch])

  useEffect(() => {
    clearPoll()
    if (!enabled || isStarting || state?.status !== "recovering") return
    timerRef.current = setTimeout(() => void refetch(), POLL_INTERVAL_MS)
    return clearPoll
  }, [clearPoll, enabled, isStarting, refetch, state, pollAttempt])

  return {
    state,
    error,
    isChecking: enabled && !state && !error,
    isStarting,
    refetch,
    startRecovery,
  }
}
