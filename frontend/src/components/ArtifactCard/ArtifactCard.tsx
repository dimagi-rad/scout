import { useEffect, useState } from "react"
import { ArrowUpRight, Ellipsis, Pencil, Trash2 } from "lucide-react"

import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader } from "@/components/ui/card"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Textarea } from "@/components/ui/textarea"
import type { ArtifactSummary, ArtifactType } from "@/store/artifactSlice"

const typeBadgeStyles: Record<ArtifactType, string> = {
  react: "bg-blue-100 text-blue-800 dark:bg-blue-900 dark:text-blue-200",
  html: "bg-orange-100 text-orange-800 dark:bg-orange-900 dark:text-orange-200",
  markdown: "bg-gray-100 text-gray-800 dark:bg-gray-900 dark:text-gray-200",
  story: "bg-cyan-100 text-cyan-800 dark:bg-cyan-950 dark:text-cyan-200",
  svg: "bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200",
}

const typeLabels: Record<ArtifactType, string> = {
  react: "Interactive",
  html: "HTML",
  markdown: "Document",
  story: "Story",
  svg: "Graphic",
}

export interface ArtifactCardProps {
  artifact: ArtifactSummary
  onOpen: () => void
  onUpdate: (data: { title?: string; description?: string }) => Promise<void>
  onDelete: () => void | Promise<void>
}

export function ArtifactCard({ artifact, onOpen, onUpdate, onDelete }: ArtifactCardProps) {
  const [editOpen, setEditOpen] = useState(false)
  const [deleteOpen, setDeleteOpen] = useState(false)
  const [title, setTitle] = useState(artifact.title)
  const [description, setDescription] = useState(artifact.description)
  const [isSaving, setIsSaving] = useState(false)
  const [isDeleting, setIsDeleting] = useState(false)
  const [editError, setEditError] = useState<string | null>(null)
  const [deleteError, setDeleteError] = useState<string | null>(null)

  useEffect(() => {
    if (!editOpen) {
      setTitle(artifact.title)
      setDescription(artifact.description)
      setEditError(null)
    }
  }, [artifact.description, artifact.title, editOpen])

  const displayDate = artifact.updated_at || artifact.created_at
  const dateLabel = artifact.updated_at !== artifact.created_at ? "Updated" : "Created"
  const formattedDate = new Date(displayDate).toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
  })

  async function handleSave(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const trimmedTitle = title.trim()
    const trimmedDescription = description.trim()

    if (!trimmedTitle) {
      setEditError("Enter a title before saving.")
      return
    }

    setIsSaving(true)
    setEditError(null)
    try {
      await onUpdate({ title: trimmedTitle, description: trimmedDescription })
      setEditOpen(false)
    } catch {
      setEditError("Couldn’t save these changes. Try again.")
    } finally {
      setIsSaving(false)
    }
  }

  async function handleDelete() {
    setIsDeleting(true)
    setDeleteError(null)
    try {
      await onDelete()
      setDeleteOpen(false)
    } catch {
      setDeleteError("Couldn’t delete this artifact. Try again.")
    } finally {
      setIsDeleting(false)
    }
  }

  return (
    <>
      <Card
        className="group relative flex h-full min-h-[15rem] min-w-0 flex-col overflow-hidden shadow-sm transition-[border-color,box-shadow,transform] duration-200 hover:-translate-y-0.5 hover:border-foreground/20 hover:shadow-md"
        data-testid={`artifact-card-${artifact.id}`}
      >
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button
              type="button"
              variant="ghost"
              size="icon-sm"
              className="absolute right-4 top-4 z-20 text-muted-foreground hover:text-foreground"
              aria-label={`Actions for ${artifact.title}`}
              data-testid={`artifact-actions-${artifact.id}`}
            >
              <Ellipsis aria-hidden="true" />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="w-44">
            <DropdownMenuItem onSelect={() => setEditOpen(true)}>
              <Pencil aria-hidden="true" />
              Edit details
            </DropdownMenuItem>
            <DropdownMenuSeparator />
            <DropdownMenuItem
              variant="destructive"
              onSelect={() => {
                setDeleteError(null)
                setDeleteOpen(true)
              }}
              data-testid={`artifact-delete-${artifact.id}`}
            >
              <Trash2 aria-hidden="true" />
              Delete artifact
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>

        <button
          type="button"
          onClick={onOpen}
          className="flex min-h-[15rem] w-full flex-1 appearance-none flex-col rounded-xl bg-transparent text-left outline-none focus-visible:ring-[3px] focus-visible:ring-inset focus-visible:ring-ring/50"
          aria-label={`Open ${artifact.title}`}
          data-testid={`artifact-open-${artifact.id}`}
        >
          <CardHeader className="min-w-0 gap-4 pb-3 pr-16">
            <div className="flex min-w-0 flex-wrap items-center gap-1.5">
              <Badge
                variant="secondary"
                className={typeBadgeStyles[artifact.artifact_type]}
              >
                {typeLabels[artifact.artifact_type]}
              </Badge>
              {artifact.version > 1 && (
                <span className="text-xs tabular-nums text-muted-foreground">
                  v{artifact.version}
                </span>
              )}
            </div>

            <h2
              className="line-clamp-2 break-words text-lg font-semibold leading-snug tracking-[-0.01em] text-card-foreground"
              data-testid={`artifact-title-${artifact.id}`}
            >
              {artifact.title}
            </h2>
          </CardHeader>

          <CardContent className="flex min-w-0 flex-1 flex-col">
            <p
              className="mb-5 line-clamp-2 break-words text-sm leading-6 text-muted-foreground"
              data-testid={`artifact-desc-${artifact.id}`}
            >
              {artifact.description || "No description yet."}
            </p>

            <div className="mt-auto flex items-center justify-between gap-4 border-t pt-4 text-sm">
              <span className="text-xs text-muted-foreground">
                {dateLabel} {formattedDate}
              </span>
              <span className="flex shrink-0 items-center gap-1 font-medium text-foreground transition-colors group-hover:text-primary">
                Open
                <ArrowUpRight className="size-4" aria-hidden="true" />
              </span>
            </div>
          </CardContent>
        </button>
      </Card>

      <Dialog open={editOpen} onOpenChange={(open) => !isSaving && setEditOpen(open)}>
        <DialogContent>
          <form onSubmit={handleSave} className="grid gap-5">
            <DialogHeader>
              <DialogTitle>Edit artifact details</DialogTitle>
              <DialogDescription>
                Update how this artifact appears in the library.
              </DialogDescription>
            </DialogHeader>

            <div className="grid gap-2">
              <Label htmlFor={`artifact-title-input-${artifact.id}`}>Title</Label>
              <Input
                id={`artifact-title-input-${artifact.id}`}
                value={title}
                onChange={(event) => setTitle(event.target.value)}
                autoFocus
                aria-invalid={!!editError && !title.trim()}
                disabled={isSaving}
                data-testid={`artifact-title-${artifact.id}-input`}
              />
            </div>

            <div className="grid gap-2">
              <Label htmlFor={`artifact-description-input-${artifact.id}`}>Description</Label>
              <Textarea
                id={`artifact-description-input-${artifact.id}`}
                value={description}
                onChange={(event) => setDescription(event.target.value)}
                rows={4}
                disabled={isSaving}
                data-testid={`artifact-desc-${artifact.id}-input`}
              />
            </div>

            {editError && (
              <p className="text-sm text-destructive" role="alert">
                {editError}
              </p>
            )}

            <DialogFooter>
              <Button
                type="button"
                variant="outline"
                onClick={() => setEditOpen(false)}
                disabled={isSaving}
              >
                Cancel
              </Button>
              <Button type="submit" disabled={isSaving}>
                {isSaving ? "Saving…" : "Save changes"}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      <AlertDialog
        open={deleteOpen}
        onOpenChange={(open) => !isDeleting && setDeleteOpen(open)}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete “{artifact.title}”?</AlertDialogTitle>
            <AlertDialogDescription>
              This permanently removes the artifact from this workspace. This action cannot be undone.
            </AlertDialogDescription>
          </AlertDialogHeader>
          {deleteError && (
            <p className="text-sm text-destructive" role="alert">
              {deleteError}
            </p>
          )}
          <AlertDialogFooter>
            <AlertDialogCancel disabled={isDeleting}>Keep artifact</AlertDialogCancel>
            <Button
              type="button"
              variant="destructive"
              onClick={handleDelete}
              disabled={isDeleting}
              data-testid={`artifact-confirm-delete-${artifact.id}`}
            >
              {isDeleting ? "Deleting…" : "Delete artifact"}
            </Button>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  )
}
