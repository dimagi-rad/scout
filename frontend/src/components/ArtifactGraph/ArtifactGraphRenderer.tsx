import { useMemo } from "react"
import Markdown from "react-markdown"
import remarkGfm from "remark-gfm"

import { buildStoryRegistry } from "./blocks"
import { useDiagnostics, useStoryEngine } from "./hooks"
import { isRecord, normalizeStoryDoc, runSemanticQuery } from "./runtime"
import type { ArtifactDetail, StoryBlock, StoryEngineApi, StoryRuntimeContext } from "./types"

interface ArtifactGraphRendererProps {
  artifact: ArtifactDetail
  workspaceId: string
}

export function ArtifactGraphRenderer({ artifact, workspaceId }: ArtifactGraphRendererProps) {
  const registry = useMemo(() => buildStoryRegistry(), [])
  const doc = useMemo(
    () => normalizeStoryDoc(isRecord(artifact.data) ? artifact.data.story_doc : undefined, artifact.title),
    [artifact.data, artifact.title],
  )
  const ctx = useMemo<StoryRuntimeContext>(
    () => ({
      runQuery: (query, _options) => runSemanticQuery(workspaceId, query),
    }),
    [workspaceId],
  )
  const engine = useStoryEngine(registry, ctx, doc)
  const visibleGroups = useMemo(() => groupVisibleBlocks(doc.blocks), [doc.blocks])

  if (!engine) {
    return null
  }

  return (
    <div className="h-full overflow-y-auto bg-background">
      <div className="mx-auto max-w-5xl px-6 py-6">
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
            ) : (
              <div
                key={`${group.key}-${index}`}
                className="grid gap-4"
                data-block-row-group={group.key}
                style={{
                  gridTemplateColumns: `repeat(auto-fit, minmax(min(100%, ${
                    group.blocks.length >= 3 ? "200px" : "300px"
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
