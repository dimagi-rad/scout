import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react"

import { StoryEngine } from "./engine"
import {
  isRefBinding,
  type BlockSpec,
  type Diagnostic,
  type OutputState,
  type StoryBlock,
  type StoryDoc,
  type StoryEngineApi,
  type StoryRuntimeContext,
} from "./types"

export function useOutput(engine: StoryEngineApi, ref: string): OutputState {
  const subscribe = useCallback((callback: () => void) => engine.subscribe(ref, callback), [engine, ref])
  const getSnapshot = useCallback(() => engine.getOutput(ref), [engine, ref])
  return useSyncExternalStore(subscribe, getSnapshot)
}

export interface ResolvedBlockInputs {
  values: Record<string, unknown>
  states: Record<string, OutputState>
  allReady: boolean
  pending: boolean
  failed: { name: string; state: OutputState } | null
}

const EMPTY_RESOLVED: ResolvedBlockInputs = {
  values: {},
  states: {},
  allReady: true,
  pending: false,
  failed: null,
}

export function useBlockInputs(engine: StoryEngineApi, block: StoryBlock): ResolvedBlockInputs {
  const bindings = block.inputs ?? {}
  const bindingsKey = JSON.stringify(bindings)
  const cache = useRef<ResolvedBlockInputs | null>(null)
  const bindingsRef = useRef(bindings)
  bindingsRef.current = bindings

  const subscribe = useCallback(
    (callback: () => void) => {
      cache.current = null
      const refs = Object.values(bindingsRef.current)
        .filter(isRefBinding)
        .map((binding) => binding.$ref)
      const unsubscribes = refs.map((ref) =>
        engine.subscribe(ref, () => {
          cache.current = null
          callback()
        }),
      )
      return () => unsubscribes.forEach((unsubscribe) => unsubscribe())
    },
    [engine, bindingsKey],
  )

  const getSnapshot = useCallback(() => {
    if (Object.keys(bindingsRef.current).length === 0) return EMPTY_RESOLVED
    if (!cache.current) {
      cache.current = computeResolvedInputs(engine, bindingsRef.current)
    }
    return cache.current
  }, [engine, bindingsKey])

  return useSyncExternalStore(subscribe, getSnapshot)
}

export function useDiagnostics(engine: StoryEngineApi): Diagnostic[] {
  const subscribe = useCallback((callback: () => void) => engine.subscribeAll(callback), [engine])
  const getSnapshot = useCallback(() => engine.getDiagnostics(), [engine])
  return useSyncExternalStore(subscribe, getSnapshot)
}

export function useStoryEngine(
  registry: Map<string, BlockSpec>,
  ctx: StoryRuntimeContext,
  doc: StoryDoc,
): StoryEngine | null {
  const [engine, setEngine] = useState<StoryEngine | null>(null)

  useEffect(() => {
    const created = new StoryEngine(registry, ctx)
    setEngine(created)
    return () => created.destroy()
  }, [registry, ctx])

  useEffect(() => {
    engine?.loadDoc(doc)
  }, [engine, doc])

  return engine
}

function computeResolvedInputs(
  engine: StoryEngineApi,
  bindings: NonNullable<StoryBlock["inputs"]>,
): ResolvedBlockInputs {
  const values: Record<string, unknown> = {}
  const states: Record<string, OutputState> = {}
  let allReady = true
  let pending = false
  let failed: ResolvedBlockInputs["failed"] = null

  for (const [name, binding] of Object.entries(bindings)) {
    if (isRefBinding(binding)) {
      const state = engine.getOutput(binding.$ref)
      states[name] = state
      values[name] = state.value
      if (state.status === "error" || state.status === "blocked") {
        allReady = false
        failed = failed ?? { name, state }
      } else if (state.status !== "ready") {
        allReady = false
        pending = true
      }
    } else {
      states[name] = { status: "ready", value: binding.value, epoch: 0 }
      values[name] = binding.value
    }
  }

  return { values, states, allReady, pending, failed }
}
