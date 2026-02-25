import { useEffect, useState, useCallback, useRef } from "react"
import { useNavigate, useParams, useLocation } from "react-router-dom"
import { Download, Loader2, Plus, Upload } from "lucide-react"
import { useAppStore } from "@/store/store"
import { Button } from "@/components/ui/button"
import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog"
import { KnowledgeList } from "./KnowledgeList"
import { KnowledgeForm } from "./KnowledgeForm"
import type { KnowledgeItem, KnowledgeType } from "@/store/knowledgeSlice"

export function KnowledgePage() {
  const { id } = useParams<{ id: string }>()
  const location = useLocation()
  const navigate = useNavigate()
  const importInputRef = useRef<HTMLInputElement>(null)

  const knowledgeItems = useAppStore((s) => s.knowledgeItems)
  const knowledgeStatus = useAppStore((s) => s.knowledgeStatus)
  const knowledgeFilter = useAppStore((s) => s.knowledgeFilter)
  const knowledgeSearch = useAppStore((s) => s.knowledgeSearch)
  const {
    fetchKnowledge,
    createKnowledge,
    updateKnowledge,
    deleteKnowledge,
    exportKnowledge,
    importKnowledge,
    setFilter,
    setSearch,
  } = useAppStore((s) => s.knowledgeActions)

  const [formOpen, setFormOpen] = useState(false)
  const [editItem, setEditItem] = useState<KnowledgeItem | null>(null)
  const [deleteItem, setDeleteItem] = useState<KnowledgeItem | null>(null)
  const [isDeleting, setIsDeleting] = useState(false)

  const isNew = location.pathname.endsWith("/new")

  useEffect(() => {
    fetchKnowledge({
      type: knowledgeFilter ?? undefined,
      search: knowledgeSearch || undefined,
    })
  }, [knowledgeFilter, knowledgeSearch, fetchKnowledge])

  useEffect(() => {
    if (isNew) {
      setEditItem(null)
      setFormOpen(true)
    }
  }, [isNew])

  useEffect(() => {
    if (id && !isNew && knowledgeItems.length > 0) {
      const item = knowledgeItems.find((i) => i.id === id)
      if (item) {
        setEditItem(item)
        setFormOpen(true)
      }
    }
  }, [id, isNew, knowledgeItems])

  const handleFilterChange = useCallback((type: KnowledgeType | null) => {
    setFilter(type)
  }, [setFilter])

  const handleSearchChange = useCallback((search: string) => {
    setSearch(search)
  }, [setSearch])

  const handleNewClick = () => {
    navigate("/knowledge/new")
  }

  const handleEdit = (item: KnowledgeItem) => {
    navigate(`/knowledge/${item.id}`)
  }

  const handleDelete = (item: KnowledgeItem) => {
    setDeleteItem(item)
  }

  const handleFormClose = (open: boolean) => {
    setFormOpen(open)
    if (!open) {
      setEditItem(null)
      if (isNew || id) {
        navigate("/knowledge")
      }
    }
  }

  const handleSave = async (data: Partial<KnowledgeItem> & { type: KnowledgeType }) => {
    if (editItem) {
      await updateKnowledge(editItem.id, data)
    } else {
      await createKnowledge(data)
    }
  }

  const handleConfirmDelete = async () => {
    if (!deleteItem || isDeleting) return

    setIsDeleting(true)
    try {
      await deleteKnowledge(deleteItem.id)
      setDeleteItem(null)
    } finally {
      setIsDeleting(false)
    }
  }

  const handleExport = async () => {
    await exportKnowledge()
  }

  const handleImport = async (e: React.ChangeEvent<HTMLInputElement>) => {
    if (!e.target.files?.[0]) return
    await importKnowledge(e.target.files[0])
    // Reset input so same file can be re-imported
    e.target.value = ""
  }

  const filteredItems = knowledgeItems

  return (
    <div className="container mx-auto px-8 py-8">
      {/* Header */}
      <div className="mb-8 flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold">Knowledge Base</h1>
          <p className="text-muted-foreground">
            Manage knowledge entries and learnings
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Button variant="outline" size="sm" onClick={handleExport} data-testid="knowledge-export">
            <Download className="mr-2 h-4 w-4" />
            Export
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={() => importInputRef.current?.click()}
            data-testid="knowledge-import"
          >
            <Upload className="mr-2 h-4 w-4" />
            Import
          </Button>
          <input
            ref={importInputRef}
            type="file"
            accept=".zip"
            className="hidden"
            onChange={handleImport}
          />
          <Button onClick={handleNewClick} data-testid="knowledge-new">
            <Plus className="mr-2 h-4 w-4" />
            New
          </Button>
        </div>
      </div>

      {/* Loading state */}
      {knowledgeStatus === "loading" && (
        <div className="text-muted-foreground">Loading knowledge items...</div>
      )}

      {/* Error state */}
      {knowledgeStatus === "error" && (
        <div className="rounded-lg border border-destructive/50 bg-destructive/10 p-4 text-destructive">
          Failed to load knowledge items. Please try again.
        </div>
      )}

      {/* List */}
      {knowledgeStatus === "loaded" && (
        <KnowledgeList
          items={filteredItems}
          filter={knowledgeFilter}
          search={knowledgeSearch}
          onFilterChange={handleFilterChange}
          onSearchChange={handleSearchChange}
          onEdit={handleEdit}
          onDelete={handleDelete}
        />
      )}

      {/* Create/Edit Form Dialog */}
      <KnowledgeForm
        open={formOpen}
        onOpenChange={handleFormClose}
        item={editItem}
        onSave={handleSave}
      />

      {/* Delete Confirmation Dialog */}
      <AlertDialog open={!!deleteItem} onOpenChange={(open) => !isDeleting && !open && setDeleteItem(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete Knowledge Item</AlertDialogTitle>
            <AlertDialogDescription>
              Are you sure you want to delete this {deleteItem?.type}? This action
              cannot be undone.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={isDeleting}>Cancel</AlertDialogCancel>
            <Button
              variant="destructive"
              onClick={handleConfirmDelete}
              disabled={isDeleting}
            >
              {isDeleting && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
              Delete
            </Button>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}
