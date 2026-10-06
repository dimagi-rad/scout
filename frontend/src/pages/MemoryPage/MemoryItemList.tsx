import { useState } from "react"
import { Loader2, Pencil, Trash2 } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Textarea } from "@/components/ui/textarea"
import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog"
import { errorText } from "./errorText"

export interface MemoryListItem {
  id: string
  content: string
  meta?: string
  canEdit: boolean
}

interface MemoryItemListProps {
  testIdPrefix: string
  items: MemoryListItem[]
  emptyText: string
  onUpdate: (id: string, content: string) => Promise<void>
  onDelete: (id: string) => Promise<void>
}

export function MemoryItemList({ testIdPrefix, items, emptyText, onUpdate, onDelete }: MemoryItemListProps) {
  const [editingId, setEditingId] = useState<string | null>(null)
  const [draft, setDraft] = useState("")
  const [saving, setSaving] = useState(false)
  const [editError, setEditError] = useState<string | null>(null)
  const [deleteItem, setDeleteItem] = useState<MemoryListItem | null>(null)
  const [deleting, setDeleting] = useState(false)
  const [deleteError, setDeleteError] = useState<string | null>(null)

  if (items.length === 0) {
    return (
      <p className="text-sm text-muted-foreground" data-testid={`${testIdPrefix}-empty`}>
        {emptyText}
      </p>
    )
  }

  const startEdit = (item: MemoryListItem) => {
    setEditingId(item.id)
    setDraft(item.content)
    setEditError(null)
  }

  const saveEdit = async () => {
    if (!editingId || saving) return
    setSaving(true)
    setEditError(null)
    try {
      await onUpdate(editingId, draft)
      setEditingId(null)
    } catch (error) {
      setEditError(errorText(error, "Couldn’t save this memory. Try again."))
    } finally {
      setSaving(false)
    }
  }

  const confirmDelete = async () => {
    if (!deleteItem || deleting) return
    setDeleting(true)
    setDeleteError(null)
    try {
      await onDelete(deleteItem.id)
      setDeleteItem(null)
    } catch (error) {
      setDeleteError(errorText(error, "Couldn’t delete this memory. Try again."))
    } finally {
      setDeleting(false)
    }
  }

  return (
    <>
      <ul className="divide-y rounded-md border" data-testid={`${testIdPrefix}-list`}>
        {items.map((item) => (
          <li key={item.id} className="p-3" data-testid={`${testIdPrefix}-item-${item.id}`}>
            {editingId === item.id ? (
              <div className="space-y-2">
                <Textarea
                  value={draft}
                  onChange={(e) => setDraft(e.target.value)}
                  aria-label="Edit memory"
                  data-testid={`${testIdPrefix}-edit-input-${item.id}`}
                />
                {editError && (
                  <p className="text-sm text-destructive" role="alert">
                    {editError}
                  </p>
                )}
                <div className="flex gap-2">
                  <Button
                    size="sm"
                    onClick={saveEdit}
                    disabled={saving || !draft.trim()}
                    data-testid={`${testIdPrefix}-save-${item.id}`}
                  >
                    {saving && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
                    Save
                  </Button>
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={() => setEditingId(null)}
                    disabled={saving}
                    data-testid={`${testIdPrefix}-cancel-${item.id}`}
                  >
                    Cancel
                  </Button>
                </div>
              </div>
            ) : (
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <p className="whitespace-pre-wrap break-words text-sm" data-testid={`${testIdPrefix}-content-${item.id}`}>
                    {item.content}
                  </p>
                  {item.meta && <p className="mt-1 text-xs text-muted-foreground">{item.meta}</p>}
                </div>
                {item.canEdit && (
                  <div className="flex shrink-0 gap-1">
                    <Button
                      size="icon"
                      variant="ghost"
                      aria-label="Edit memory"
                      onClick={() => startEdit(item)}
                      data-testid={`${testIdPrefix}-edit-${item.id}`}
                    >
                      <Pencil className="h-4 w-4" />
                    </Button>
                    <Button
                      size="icon"
                      variant="ghost"
                      aria-label="Delete memory"
                      onClick={() => {
                        setDeleteError(null)
                        setDeleteItem(item)
                      }}
                      data-testid={`${testIdPrefix}-delete-${item.id}`}
                    >
                      <Trash2 className="h-4 w-4" />
                    </Button>
                  </div>
                )}
              </div>
            )}
          </li>
        ))}
      </ul>

      <AlertDialog open={!!deleteItem} onOpenChange={(open) => !deleting && !open && setDeleteItem(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete memory</AlertDialogTitle>
            <AlertDialogDescription>
              Scout will stop applying “{deleteItem?.content}” in future conversations.
            </AlertDialogDescription>
          </AlertDialogHeader>
          {deleteError && (
            <p className="text-sm text-destructive" role="alert">
              {deleteError}
            </p>
          )}
          <AlertDialogFooter>
            <AlertDialogCancel disabled={deleting}>Cancel</AlertDialogCancel>
            <Button
              variant="destructive"
              onClick={confirmDelete}
              disabled={deleting}
              data-testid={`${testIdPrefix}-confirm-delete`}
            >
              {deleting && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
              Delete
            </Button>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  )
}
