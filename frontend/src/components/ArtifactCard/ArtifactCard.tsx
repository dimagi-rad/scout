import { useCallback, useEffect, useRef, useState } from "react"
import { Eye, Trash2, Zap } from "lucide-react"

import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader } from "@/components/ui/card"
import { cn } from "@/lib/utils"
import type { ArtifactSummary, ArtifactType } from "@/store/artifactSlice"

const typeBadgeStyles: Record<ArtifactType, string> = {
  react: "bg-blue-100 text-blue-800 dark:bg-blue-900 dark:text-blue-200",
  html: "bg-orange-100 text-orange-800 dark:bg-orange-900 dark:text-orange-200",
  markdown: "bg-gray-100 text-gray-800 dark:bg-gray-900 dark:text-gray-200",
  plotly: "bg-purple-100 text-purple-800 dark:bg-purple-900 dark:text-purple-200",
  story: "bg-cyan-100 text-cyan-800 dark:bg-cyan-950 dark:text-cyan-200",
  svg: "bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200",
}

export interface ArtifactCardProps {
  artifact: ArtifactSummary
  onOpen: () => void
  onUpdate: (data: { title?: string; description?: string }) => Promise<void>
  onDelete: () => void
}

export function ArtifactCard({ artifact, onOpen, onUpdate, onDelete }: ArtifactCardProps) {
  const [confirmDelete, setConfirmDelete] = useState(false)

  const formattedDate = new Date(artifact.created_at).toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
  })

  return (
    <Card
      className="flex h-full min-h-[17rem] min-w-0 flex-col overflow-hidden"
      data-testid={`artifact-card-${artifact.id}`}
    >
      <CardHeader className="min-w-0 pb-2">
        <div className="min-w-0 space-y-3">
          <div className="flex min-w-0 flex-wrap items-center gap-1.5">
            <Badge
              variant="secondary"
              className={typeBadgeStyles[artifact.artifact_type]}
            >
              {artifact.artifact_type}
            </Badge>
            {artifact.has_live_queries && (
              <Badge variant="outline" className="gap-1">
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
            className="text-base font-semibold leading-snug text-card-foreground"
            inputClassName="text-base font-semibold leading-snug"
            displayClassName="line-clamp-2 break-words"
            data-testid={`artifact-title-${artifact.id}`}
          />
        </div>
      </CardHeader>

      <CardContent className="flex min-w-0 flex-1 flex-col">
        <EditableText
          value={artifact.description}
          placeholder="Add a description..."
          onSave={(description) => onUpdate({ description })}
          className="mb-4 text-sm leading-6 text-muted-foreground"
          inputClassName="text-sm leading-6"
          displayClassName="line-clamp-2 break-words"
          multiline
          data-testid={`artifact-desc-${artifact.id}`}
        />

        <div className="mb-4 text-xs text-muted-foreground">
          Created {formattedDate}
        </div>

        <div className="mt-auto border-t pt-3">
          {confirmDelete ? (
            <div className="grid grid-cols-2 gap-2">
              <Button
                variant="destructive"
                size="sm"
                onClick={onDelete}
                className="min-w-0"
                data-testid={`artifact-confirm-delete-${artifact.id}`}
              >
                Confirm
              </Button>
              <Button
                variant="outline"
                size="sm"
                onClick={() => setConfirmDelete(false)}
                className="min-w-0"
                data-testid={`artifact-cancel-delete-${artifact.id}`}
              >
                Cancel
              </Button>
            </div>
          ) : (
            <div className="grid grid-cols-2 gap-2">
              <Button
                variant="ghost"
                size="sm"
                onClick={onOpen}
                className="min-w-0"
                data-testid={`artifact-open-${artifact.id}`}
              >
                <Eye className="h-4 w-4" />
                View
              </Button>
              <Button
                variant="destructive"
                size="sm"
                onClick={() => setConfirmDelete(true)}
                className="min-w-0"
                data-testid={`artifact-delete-${artifact.id}`}
              >
                <Trash2 className="h-4 w-4" />
                Delete
              </Button>
            </div>
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
  displayClassName,
  multiline,
  "data-testid": testId,
}: {
  value: string
  placeholder?: string
  onSave: (value: string) => Promise<void>
  className?: string
  inputClassName?: string
  displayClassName?: string
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
      onChange: (e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) =>
        setDraft(e.target.value),
      onBlur: commit,
      onKeyDown: handleKeyDown,
      className: cn(
        "w-full rounded-md border border-input bg-background px-2 py-1",
        inputClassName,
      ),
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
    <button
      type="button"
      onClick={() => setEditing(true)}
      className={cn(
        "block w-full min-w-0 rounded-md px-1 text-left transition-colors hover:bg-muted",
        isEmpty && "italic text-muted-foreground/50",
        className,
      )}
      title="Click to edit"
      data-testid={testId}
    >
      <span className={cn("block min-w-0", displayClassName)}>{displayValue}</span>
    </button>
  )
}
