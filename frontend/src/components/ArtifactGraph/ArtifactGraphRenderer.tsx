import { useEffect, useMemo, useRef, type Ref } from "react"
import Markdown from "react-markdown"
import remarkGfm from "remark-gfm"

import { buildStoryRegistry } from "./blocks"
import { useDiagnostics, useStoryEngine } from "./hooks"
import { ArtifactDateContext, isRecord, normalizeStoryDoc, runSemanticQuery } from "./runtime"
import type { ArtifactDetail, DateRange, StoryBlock, StoryEngineApi, StoryRuntimeContext } from "./types"

interface ArtifactGraphRendererProps {
  artifact: ArtifactDetail
  workspaceId: string
  dataRevision?: string
  containerRef?: Ref<HTMLDivElement>
  onDateSourcesChange?: (sources: Record<string, DateRange | null>) => void
}

export function ArtifactGraphRenderer({ artifact, workspaceId, containerRef, dataRevision, onDateSourcesChange }: ArtifactGraphRendererProps) {
  const registry = useMemo(() => buildStoryRegistry(artifact.date_context), [artifact.date_context])
  const doc = useMemo(
    () => normalizeStoryDoc(isRecord(artifact.data) ? artifact.data.story_doc : undefined, artifact.title),
    [artifact.data, artifact.title],
  )
  const ctx = useMemo<StoryRuntimeContext>(
    () => ({
      runQuery: (query) => runSemanticQuery(workspaceId, query, artifact.date_context),
    }),
    [workspaceId, artifact.date_context],
  )
  const engine = useStoryEngine(registry, ctx, doc)
  useEffect(() => {
    if (!engine || !onDateSourcesChange) return
    let last = ""
    const publish = () => {
      const sources: Record<string, DateRange | null> = {}
      for (const block of doc.blocks) {
        const port = block.type === "date_filter" ? "value" : block.type === "period_selector" ? "current" : null
        if (!port) continue
        const state = engine.getOutput(`${block.id}.${port}`)
        if (state.status !== "ready") {
          // An omitted override means "use the saved default" on the server.
          // Keep failed selections explicit so inspection cannot substitute it.
          if (state.status === "error" || state.status === "blocked") sources[block.id] = null
          continue
        }
        const value = state.value
        if (isRecord(value) && typeof value.start === "string" && typeof value.end === "string") {
          sources[block.id] = { start: value.start, end: value.end }
        }
      }
      const fingerprint = JSON.stringify(sources)
      if (fingerprint !== last) {
        last = fingerprint
        onDateSourcesChange(sources)
      }
    }
    publish()
    return engine.subscribeAll(publish)
  }, [engine, doc, onDateSourcesChange])
  const lastPublication = useRef({ engine, dataRevision })
  useEffect(() => {
    if (lastPublication.current.engine === engine && lastPublication.current.dataRevision !== dataRevision) {
      engine?.refreshData()
    }
    lastPublication.current = { engine, dataRevision }
  }, [engine, dataRevision])
  const visibleGroups = useMemo(() => groupVisibleBlocks(doc.blocks), [doc.blocks])

  if (!engine) {
    return null
  }

  return (
    <ArtifactDateContext.Provider value={artifact.date_context}>
      <div ref={containerRef} data-artifact-story className="h-full overflow-y-auto bg-background">
        <div data-artifact-story-content className="mx-auto max-w-5xl px-6 py-6">
          <Diagnostics engine={engine} />
          {doc.prd && (
            <div className="mb-5 border-l-2 border-primary/40 pl-3 text-xs text-muted-foreground">
              <Markdown remarkPlugins={[remarkGfm]}>{doc.prd}</Markdown>
            </div>
          )}
          <div className="space-y-4">
            {visibleGroups.map((group, index) =>
              group.blocks.length === 1 ? (
                <RenderedBlock
                  key={group.blocks[0].id}
                  block={group.blocks[0]}
                  engine={engine}
                  registry={registry}
                />
              ) : group.blocks.every((block) => block.type === "stat") ? (
                <StatGroup
                  key={`${group.key}-${index}`}
                  blocks={group.blocks}
                  engine={engine}
                  groupKey={group.key}
                  registry={registry}
                />
              ) : (
                <div
                  key={`${group.key}-${index}`}
                  className="grid gap-4"
                  data-block-row-group={group.key}
                  style={{
                    gridTemplateColumns: `repeat(auto-fit, minmax(min(100%, ${
                      group.blocks.length >= 3 ? "230px" : "300px"
                    }), 1fr))`,
                  }}
                >
                  {group.blocks.map((block) => (
                    <RenderedBlock key={block.id} block={block} engine={engine} registry={registry} />
                  ))}
                </div>
              ),
            )}
          </div>
        </div>
      </div>
    </ArtifactDateContext.Provider>
  )
}

function StatGroup({
  blocks,
  engine,
  groupKey,
  registry,
}: {
  blocks: StoryBlock[]
  engine: StoryEngineApi
  groupKey: string
  registry: ReturnType<typeof buildStoryRegistry>
}) {
  const comparisonContext = statGroupComparisonContext(blocks)
  const periodVisibility = comparisonContext ? "[&_[data-stat-period]]:hidden" : ""

  return (
    <section data-stat-group className="overflow-hidden rounded-xl border border-border bg-border">
      <header className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1 bg-card px-4 py-3">
        <h2 className="text-sm font-semibold">Key metrics</h2>
        {comparisonContext && <p className="text-xs text-muted-foreground">{comparisonContext}</p>}
      </header>
      <div
        className={`grid gap-px bg-border [&_[data-block-type=stat]]:min-h-28 [&_[data-block-type=stat]]:rounded-none [&_[data-block-type=stat]]:border-0 ${periodVisibility}`}
        data-block-row-group={groupKey}
        style={{
          gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 210px), 1fr))",
        }}
      >
        {blocks.map((block) => (
          <RenderedBlock key={block.id} block={block} engine={engine} registry={registry} />
        ))}
      </div>
    </section>
  )
}

function Diagnostics({ engine }: { engine: StoryEngineApi }) {
  const diagnostics = useDiagnostics(engine)
  const errors = diagnostics.filter((item) => item.severity === "error")

  if (errors.length === 0) return null

  return (
    <div className="mb-4 rounded-md border border-destructive/30 bg-destructive/5 px-3 py-2 text-sm text-destructive">
      {errors.slice(0, 3).map((error, index) => (
        <div key={`${error.blockId ?? "doc"}-${index}`}>
          {error.blockId ? `${error.blockId}: ` : ""}
          {error.message}
        </div>
      ))}
    </div>
  )
}

function RenderedBlock({
  block,
  engine,
  registry,
}: {
  block: StoryBlock
  engine: StoryEngineApi
  registry: ReturnType<typeof buildStoryRegistry>
}) {
  const spec = registry.get(block.type)
  const Component = spec?.component
  if (!Component) return null
  return <Component block={block} config={block.config ?? {}} engine={engine} />
}

function statGroupComparisonContext(blocks: StoryBlock[]): string | null {
  const labels = blocks.map((block) => {
    const comparison = isRecord(block.config?.comparison) ? block.config.comparison : null
    return comparison && comparison.type !== "none" && typeof comparison.label === "string"
      ? comparison.label.trim()
      : null
  })
  const first = labels[0]
  if (!first || labels.some((label) => label !== first)) return null
  return first.replace(/^vs\s+/i, "Compared with ")
}

function groupVisibleBlocks(blocks: StoryBlock[]) {
  const groups: Array<{ key: string; blocks: StoryBlock[] }> = []
  let currentGroup: string | null = null

  for (const block of blocks) {
    if (block.hidden) {
      currentGroup = null
      continue
    }
    const key = block.row_group ?? block.id
    const last = groups[groups.length - 1]
    if (block.row_group && block.row_group === currentGroup && last?.key === block.row_group) {
      last.blocks.push(block)
    } else {
      groups.push({ key, blocks: [block] })
    }
    currentGroup = block.row_group ?? null
  }
  return groups
}
