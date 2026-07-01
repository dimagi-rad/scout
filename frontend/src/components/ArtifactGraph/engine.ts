import {
  isLiteralBinding,
  isRefBinding,
  outputKey,
  parseRef,
  type BlockPorts,
  type BlockSpec,
  type Diagnostic,
  type OutputState,
  type StoryBlock,
  type StoryDoc,
  type StoryEngineApi,
  type StoryRuntimeContext,
} from "./types"

const IDLE: OutputState = { status: "idle", epoch: 0 }

interface NodeRuntime {
  id: string
  block: StoryBlock
  spec: BlockSpec
  ports: BlockPorts
  deps: Map<string, string>
  literals: Map<string, unknown>
  unbound: string[]
  configError: string | null
  signature: string
  epoch: number
  abort: AbortController | null
  debounceTimer: ReturnType<typeof setTimeout> | null
  lastSnapshot: string | null
  lastOk: boolean
}

export class StoryEngine implements StoryEngineApi {
  private registry: Map<string, BlockSpec>
  private ctx: StoryRuntimeContext
  private nodes = new Map<string, NodeRuntime>()
  private outputs = new Map<string, OutputState>()
  private dependents = new Map<string, Set<string>>()
  private listeners = new Map<string, Set<() => void>>()
  private allListeners = new Set<() => void>()
  private diagnostics: Diagnostic[] = []
  private destroyed = false

  constructor(registry: Map<string, BlockSpec>, ctx: StoryRuntimeContext) {
    this.registry = registry
    this.ctx = ctx
  }

  loadDoc(doc: StoryDoc): void {
    if (this.destroyed) return

    const oldNodes = this.nodes
    const oldOutputs = this.outputs
    for (const node of oldNodes.values()) {
      this.cancelEval(node)
    }

    this.nodes = new Map()
    this.outputs = new Map()
    this.dependents = new Map()
    this.diagnostics = []

    for (const block of doc.blocks) {
      if (this.nodes.has(block.id)) {
        this.diagnostics.push({
          severity: "error",
          blockId: block.id,
          message: `Duplicate block id "${block.id}"`,
        })
        continue
      }
      const spec = this.registry.get(block.type)
      if (!spec) {
        this.diagnostics.push({
          severity: "error",
          blockId: block.id,
          message: `Unknown block type "${block.type}"`,
        })
        continue
      }
      this.nodes.set(block.id, this.buildNode(block, spec))
    }

    for (const node of this.nodes.values()) {
      this.validateRefs(node)
    }
    this.detectCycles()

    for (const node of this.nodes.values()) {
      const previous = oldNodes.get(node.id)
      const carried = previous && previous.signature === node.signature && !node.configError

      if (node.configError) {
        this.publishStatusAll(node, "error", node.configError)
        continue
      }

      if (carried) {
        node.lastSnapshot = previous.lastSnapshot
        node.lastOk = previous.lastOk
        for (const port of node.ports.outputs) {
          const key = outputKey(node.id, port.name)
          const old = oldOutputs.get(key)
          if (old) this.outputs.set(key, old)
        }
        continue
      }

      if (node.spec.initialOutputs) {
        const values = node.spec.initialOutputs(node.block.config ?? {})
        for (const port of node.ports.outputs) {
          this.outputs.set(outputKey(node.id, port.name), {
            status: "ready",
            value: values[port.name],
            epoch: 1,
          })
        }
      }
    }

    this.notifyAllKeys()
    for (const node of this.nodes.values()) {
      this.reconcile(node.id)
    }
  }

  getOutput(ref: string): OutputState {
    return this.outputs.get(ref) ?? IDLE
  }

  subscribe(ref: string, callback: () => void): () => void {
    let listeners = this.listeners.get(ref)
    if (!listeners) {
      listeners = new Set()
      this.listeners.set(ref, listeners)
    }
    listeners.add(callback)
    return () => listeners?.delete(callback)
  }

  subscribeAll(callback: () => void): () => void {
    this.allListeners.add(callback)
    return () => this.allListeners.delete(callback)
  }

  setSourceOutputs(blockId: string, values: Record<string, unknown>): void {
    if (this.destroyed) return
    const node = this.nodes.get(blockId)
    if (!node) return

    const changedPorts: string[] = []
    for (const [port, value] of Object.entries(values)) {
      const key = outputKey(blockId, port)
      const current = this.outputs.get(key)
      if (current?.status === "ready" && jsonEqual(current.value, value)) continue
      this.outputs.set(key, {
        status: "ready",
        value,
        epoch: (current?.epoch ?? 0) + 1,
      })
      changedPorts.push(port)
    }

    if (changedPorts.length === 0) return
    for (const port of changedPorts) {
      this.notifyKey(outputKey(blockId, port))
    }
    this.notifyAllListeners()
    this.onOutputsChanged(blockId, changedPorts)
  }

  getDiagnostics(): Diagnostic[] {
    return this.diagnostics
  }

  destroy(): void {
    this.destroyed = true
    for (const node of this.nodes.values()) {
      this.cancelEval(node)
    }
    this.listeners.clear()
    this.allListeners.clear()
  }

  private buildNode(block: StoryBlock, spec: BlockSpec): NodeRuntime {
    const node: NodeRuntime = {
      id: block.id,
      block,
      spec,
      ports: { inputs: [], outputs: [] },
      deps: new Map(),
      literals: new Map(),
      unbound: [],
      configError: null,
      signature: JSON.stringify({
        type: block.type,
        config: block.config ?? {},
        inputs: block.inputs ?? {},
      }),
      epoch: 0,
      abort: null,
      debounceTimer: null,
      lastSnapshot: null,
      lastOk: false,
    }

    try {
      node.ports = spec.ports(block.config ?? {}, block.inputs ?? {})
    } catch (error) {
      node.configError = `Invalid config: ${message(error)}`
      this.diagnostics.push({ severity: "error", blockId: block.id, message: node.configError })
      return node
    }

    const bindings = block.inputs ?? {}
    for (const port of node.ports.inputs) {
      const binding = bindings[port.name]
      if (binding === undefined) {
        if (port.required) {
          node.configError = `Required input "${port.name}" is not bound`
          this.diagnostics.push({ severity: "error", blockId: block.id, message: node.configError })
        } else {
          node.unbound.push(port.name)
        }
        continue
      }
      if (isRefBinding(binding)) {
        node.deps.set(port.name, binding.$ref)
      } else if (isLiteralBinding(binding)) {
        node.literals.set(port.name, binding.value)
      } else {
        node.configError = `Input "${port.name}" has a malformed binding`
        this.diagnostics.push({ severity: "error", blockId: block.id, message: node.configError })
      }
    }

    const declared = new Set(node.ports.inputs.map((port) => port.name))
    for (const name of Object.keys(bindings)) {
      if (!declared.has(name)) {
        this.diagnostics.push({
          severity: "warning",
          blockId: block.id,
          message: `Binding "${name}" does not match any input of ${block.type}`,
        })
      }
    }

    return node
  }

  private validateRefs(node: NodeRuntime): void {
    if (node.configError) return
    for (const [inputName, ref] of node.deps) {
      const parsed = parseRef(ref)
      const producer = parsed ? this.nodes.get(parsed.blockId) : undefined
      const hasPort = producer && !producer.configError
        ? producer.ports.outputs.some((port) => port.name === parsed!.port)
        : false
      if (!parsed || !producer || !hasPort) {
        node.configError = `Input "${inputName}" references "${ref}" which does not exist`
        this.diagnostics.push({ severity: "error", blockId: node.id, message: node.configError })
        return
      }
      let dependents = this.dependents.get(ref)
      if (!dependents) {
        dependents = new Set()
        this.dependents.set(ref, dependents)
      }
      dependents.add(node.id)
    }
  }

  private detectCycles(): void {
    const visiting = new Set<string>()
    const visited = new Set<string>()
    const inCycle = new Set<string>()
    const stack: string[] = []

    const visit = (id: string): void => {
      if (visited.has(id)) return
      visiting.add(id)
      stack.push(id)
      const node = this.nodes.get(id)
      if (node && !node.configError) {
        for (const ref of node.deps.values()) {
          const parsed = parseRef(ref)
          if (!parsed) continue
          if (visiting.has(parsed.blockId)) {
            const start = stack.indexOf(parsed.blockId)
            for (const member of stack.slice(Math.max(start, 0))) {
              inCycle.add(member)
            }
          } else {
            visit(parsed.blockId)
          }
        }
      }
      stack.pop()
      visiting.delete(id)
      visited.add(id)
    }

    for (const id of this.nodes.keys()) {
      visit(id)
    }

    if (inCycle.size === 0) return
    const members = [...inCycle].sort().join(", ")
    this.diagnostics.push({ severity: "error", message: `Circular dependency between blocks: ${members}` })
    for (const id of inCycle) {
      const node = this.nodes.get(id)
      if (node && !node.configError) {
        node.configError = `Part of a circular dependency (${members})`
      }
    }
  }

  private onOutputsChanged(blockId: string, ports: string[]): void {
    const toReconcile = new Set<string>()
    for (const port of ports) {
      const dependents = this.dependents.get(outputKey(blockId, port))
      if (!dependents) continue
      for (const id of dependents) {
        toReconcile.add(id)
      }
    }
    for (const id of toReconcile) {
      this.reconcile(id)
    }
  }

  private reconcile(nodeId: string): void {
    if (this.destroyed) return
    const node = this.nodes.get(nodeId)
    if (!node || !node.spec.evaluate || node.configError) return

    let failedRef: string | null = null
    let failedError: string | undefined
    let allReady = true
    const values: Record<string, unknown> = {}

    for (const port of node.ports.inputs) {
      if (node.literals.has(port.name)) {
        values[port.name] = node.literals.get(port.name)
        continue
      }
      if (node.unbound.includes(port.name)) {
        values[port.name] = undefined
        continue
      }
      const ref = node.deps.get(port.name)
      if (!ref) continue
      const state = this.getOutput(ref)
      if (state.status === "error" || state.status === "blocked") {
        failedRef = ref
        failedError = state.error
        break
      }
      if (state.status !== "ready") {
        allReady = false
      } else {
        values[port.name] = state.value
      }
    }

    if (failedRef) {
      this.cancelEval(node)
      this.publishStatusAll(node, "blocked", `Waiting on "${failedRef}", which failed${failedError ? `: ${failedError}` : ""}`)
      return
    }
    if (!allReady) {
      this.cancelEval(node)
      this.publishStatusAll(node, "pending")
      return
    }

    const snapshot = JSON.stringify(values)
    if (snapshot === node.lastSnapshot && node.lastOk) return
    this.scheduleEval(node, values, snapshot)
  }

  private scheduleEval(node: NodeRuntime, values: Record<string, unknown>, snapshot: string): void {
    this.cancelEval(node)
    this.publishStatusAll(node, "pending")

    const start = () => {
      node.debounceTimer = null
      void this.runEval(node, values, snapshot)
    }
    if (node.spec.debounceMs && node.spec.debounceMs > 0) {
      node.debounceTimer = setTimeout(start, node.spec.debounceMs)
    } else {
      start()
    }
  }

  private async runEval(node: NodeRuntime, values: Record<string, unknown>, snapshot: string): Promise<void> {
    if (this.destroyed) return
    node.epoch += 1
    const epoch = node.epoch
    const abort = new AbortController()
    node.abort = abort
    node.lastSnapshot = snapshot

    try {
      const result = await node.spec.evaluate!({
        blockId: node.id,
        config: node.block.config ?? {},
        inputs: values,
        ctx: this.ctx,
        signal: abort.signal,
      })
      if (this.destroyed || epoch !== node.epoch) return
      node.lastOk = true
      node.abort = null
      this.publishOutputs(node, result)
    } catch (error) {
      if (this.destroyed || epoch !== node.epoch || abort.signal.aborted) return
      node.lastOk = false
      node.abort = null
      this.publishStatusAll(node, "error", message(error))
    }
  }

  private cancelEval(node: NodeRuntime): void {
    if (node.debounceTimer !== null) {
      clearTimeout(node.debounceTimer)
      node.debounceTimer = null
    }
    if (node.abort) {
      node.abort.abort()
      node.abort = null
    }
  }

  private publishOutputs(node: NodeRuntime, values: Record<string, unknown>): void {
    const changed: string[] = []
    for (const port of node.ports.outputs) {
      const key = outputKey(node.id, port.name)
      const current = this.outputs.get(key)
      this.outputs.set(key, {
        status: "ready",
        value: values[port.name],
        epoch: (current?.epoch ?? 0) + 1,
      })
      changed.push(port.name)
      this.notifyKey(key)
    }
    this.notifyAllListeners()
    this.onOutputsChanged(node.id, changed)
  }

  private publishStatusAll(node: NodeRuntime, status: "pending" | "error" | "blocked", error?: string): void {
    const changed: string[] = []
    for (const port of node.ports.outputs) {
      const key = outputKey(node.id, port.name)
      const current = this.outputs.get(key)
      if (current?.status === status && current.error === error) continue
      this.outputs.set(key, {
        status,
        value: current?.value,
        error,
        epoch: (current?.epoch ?? 0) + 1,
      })
      changed.push(port.name)
      this.notifyKey(key)
    }
    if (changed.length === 0) return
    this.notifyAllListeners()
    this.onOutputsChanged(node.id, changed)
  }

  private notifyKey(key: string): void {
    const listeners = this.listeners.get(key)
    if (!listeners) return
    for (const callback of [...listeners]) {
      callback()
    }
  }

  private notifyAllKeys(): void {
    for (const key of this.listeners.keys()) {
      this.notifyKey(key)
    }
    this.notifyAllListeners()
  }

  private notifyAllListeners(): void {
    for (const callback of [...this.allListeners]) {
      callback()
    }
  }
}

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

function jsonEqual(a: unknown, b: unknown): boolean {
  return JSON.stringify(a) === JSON.stringify(b)
}
