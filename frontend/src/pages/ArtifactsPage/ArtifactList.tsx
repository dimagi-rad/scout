import { useState, useRef, useEffect, useCallback } from "react"
import { Eye, Trash2, Search, Zap } from "lucide-react"
import { useAppStore } from "@/store/store"
import { Card, CardHeader, CardContent } from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import type { ArtifactSummary } from "@/store/artifactSlice"

const typeBadgeStyles: Record<string, string> = {
  react: "bg-blue-100 text-blue-800 dark:bg-blue-900 dark:text-blue-200",
  html: "bg-orange-100 text-orange-800 dark:bg-orange-900 dark:text-orange-200",
  markdown: "bg-gray-100 text-gray-800 dark:bg-gray-900 dark:text-gray-200",
  plotly: "bg-purple-100 text-purple-800 dark:bg-purple-900 dark:text-purple-200",
  svg: "bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200",
}

interface ArtifactListProps {
  items: ArtifactSummary[]
  search: string
  onSearchChange: (search: string) => void
  onUpdate: (item: ArtifactSummary, data: { title?: string; description?: string }) => Promise<void>
  onDelete: (item: ArtifactSummary) => void
}

export function ArtifactList({ items, search, onSearchChange, onUpdate, onDelete }: ArtifactListProps) {
  const openArtifact = useAppStore((s) => s.uiActions.openArtifact)

  function handleOpen(artifact: ArtifactSummary) {
    openArtifact(artifact.id)
  }

  if (items.length === 0 && !search) {
    return (
      <div className="rounded-lg border border-dashed p-8 text-center">
        <p className="text-muted-foreground">
          No artifacts yet. Artifacts are created by the AI agent during chat conversations.
        </p>
      </div>
    )
  }

  return (
    <div className="space-y-4">
      {/* Search */}
      <div className="relative max-w-sm">
        <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
        <Input
          value={search}
          onChange={(e) => onSearchChange(e.target.value)}
          placeholder="Search artifacts..."
          className="pl-9"
          data-testid="artifact-search"
        />
      </div>

      {items.length === 0 && search && (
        <div className="rounded-lg border border-dashed p-8 text-center">
          <p className="text-muted-foreground">
            No artifacts match "{search}"
          </p>
        </div>
      )}

      {/* Card grid */}
      <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
        {items.map((item) => (
          <ArtifactCard
            key={item.id}
            artifact={item}
            onOpen={() => handleOpen(item)}
            onUpdate={(data) => onUpdate(item, data)}
            onDelete={() => onDelete(item)}
          />
        ))}
      </div>
    </div>
  )
}

function ArtifactCard({
  artifact,
  onOpen,
  onUpdate,
  onDelete,
}: {
  artifact: ArtifactSummary
  onOpen: () => void
  onUpdate: (data: { title?: string; description?: string }) => Promise<void>
  onDelete: () => void
}) {
  const [confirmDelete, setConfirmDelete] = useState(false)

  const formattedDate = new Date(artifact.created_at).toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
  })

  return (
    <Card className="flex flex-col" data-testid={`artifact-card-${artifact.id}`}>
      <CardHeader className="pb-2">
        <div className="flex items-start justify-between gap-2">
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-2 mb-1">
              <Badge
                variant="secondary"
                className={typeBadgeStyles[artifact.artifact_type] ?? ""}
              >
                {artifact.artifact_type}
              </Badge>
              {artifact.has_live_queries && (
                <Badge variant="outline" className="text-xs gap-1">
                  <Zap className="h-3 w-3" />
                  Live
                </Badge>
              )}
              {artifact.version > 1 && (
                <span className="text-xs text-muted-foreground">
                  v{artifact.version}
                </span>
              )}
            </div>
            <EditableText
              value={artifact.title}
              onSave={(title) => onUpdate({ title })}
              className="font-medium"
              inputClassName="font-medium"
              data-testid={`artifact-title-${artifact.id}`}
            />
          </div>
        </div>
      </CardHeader>
      <CardContent className="flex-1 flex flex-col">
        <EditableText
          value={artifact.description}
          placeholder="Add a description..."
          onSave={(description) => onUpdate({ description })}
          className="text-sm text-muted-foreground line-clamp-2 mb-3"
          inputClassName="text-sm"
          multiline
          data-testid={`artifact-desc-${artifact.id}`}
        />

        <div className="text-xs text-muted-foreground mb-3">
          Created {formattedDate}
        </div>

        {/* Actions */}
        <div className="mt-auto flex items-center gap-2 pt-2 border-t">
          <Button variant="ghost" size="sm" onClick={onOpen} data-testid={`artifact-open-${artifact.id}`}>
            <Eye className="mr-1 h-4 w-4" />
            View
          </Button>
          {confirmDelete ? (
            <div className="flex items-center gap-1 ml-auto">
              <Button
                variant="destructive"
                size="sm"
                onClick={onDelete}
              >
                Confirm
              </Button>
              <Button
                variant="ghost"
                size="sm"
                onClick={() => setConfirmDelete(false)}
              >
                Cancel
              </Button>
            </div>
          ) : (
            <Button
              variant="ghost"
              size="sm"
              onClick={() => setConfirmDelete(true)}
              className="text-destructive hover:text-destructive"
            >
              <Trash2 className="mr-1 h-4 w-4" />
              Delete
            </Button>
          )}
        </div>
      </CardContent>
    </Card>
  )
}

function EditableText({
  value,
  placeholder,
  onSave,
  className,
  inputClassName,
  multiline,
  "data-testid": testId,
}: {
  value: string
  placeholder?: string
  onSave: (value: string) => Promise<void>
  className?: string
  inputClassName?: string
  multiline?: boolean
  "data-testid"?: string
}) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(value)
  const inputRef = useRef<HTMLInputElement | HTMLTextAreaElement>(null)

  useEffect(() => {
    setDraft(value)
  }, [value])

  useEffect(() => {
    if (editing && inputRef.current) {
      inputRef.current.focus()
      inputRef.current.select()
    }
  }, [editing])

  const commit = useCallback(async () => {
    const trimmed = draft.trim()
    setEditing(false)
    if (trimmed !== value) {
      await onSave(trimmed)
    }
  }, [draft, value, onSave])

  const handleKeyDown = useCallback((e: React.KeyboardEvent) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault()
      commit()
    } else if (e.key === "Escape") {
      setDraft(value)
      setEditing(false)
    }
  }, [commit, value])

  if (editing) {
    const sharedProps = {
      ref: inputRef as never,
      value: draft,
      onChange: (e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) => setDraft(e.target.value),
      onBlur: commit,
      onKeyDown: handleKeyDown,
      className: `w-full rounded-md border border-input bg-background px-2 py-1 ${inputClassName ?? ""}`,
      "data-testid": testId ? `${testId}-input` : undefined,
    }

    if (multiline) {
      return <textarea {...sharedProps} rows={2} />
    }
    return <input type="text" {...sharedProps} />
  }

  const displayValue = value || placeholder
  const isEmpty = !value

  return (
    <span
      onClick={() => setEditing(true)}
      className={`cursor-pointer rounded-md px-1 -mx-1 hover:bg-muted transition-colors ${isEmpty ? "italic text-muted-foreground/50" : ""} ${className ?? ""}`}
      title="Click to edit"
      data-testid={testId}
    >
      {displayValue}
    </span>
  )
}
