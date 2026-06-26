import { useEffect, useState, useCallback, useRef } from "react"
import { useNavigate, useParams, useLocation } from "react-router-dom"
import { Download, Loader2, Plus, Upload } from "lucide-react"
import { useAppStore } from "@/store/store"
import { useNetworkStatus } from "@/hooks/useNetworkStatus"
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

  const activeDomainId = useAppStore((s) => s.activeDomainId)
  const knowledgeItems = useAppStore((s) => s.knowledgeItems)
  const knowledgeStatus = useAppStore((s) => s.knowledgeStatus)
  const knowledgeFilter = useAppStore((s) => s.knowledgeFilter)
  const knowledgeSearch = useAppStore((s) => s.knowledgeSearch)
  const knowledgePagination = useAppStore((s) => s.knowledgePagination)
  // Surface server-side pagination so items beyond page 1 are reachable
  // (arch #254, 05#7 — the backend now paginates in the DB query).
  const [page, setPage] = useState(1)
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

  const { status: networkStatus } = useNetworkStatus()
  const [formOpen, setFormOpen] = useState(false)
  const [editItem, setEditItem] = useState<KnowledgeItem | null>(null)
  const [deleteItem, setDeleteItem] = useState<KnowledgeItem | null>(null)
  const [isDeleting, setIsDeleting] = useState(false)

  const isNew = location.pathname.endsWith("/new")

  // Reset to the first page whenever the filter or search changes.
  useEffect(() => {
    setPage(1)
  }, [knowledgeFilter, knowledgeSearch, activeDomainId])

  useEffect(() => {
    if (!activeDomainId) return
    fetchKnowledge({
      type: knowledgeFilter ?? undefined,
      search: knowledgeSearch || undefined,
      page,
    })
  }, [activeDomainId, knowledgeFilter, knowledgeSearch, page, fetchKnowledge])

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
      {knowledgeStatus === "error" && networkStatus === "online" && (
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

      {/* Pagination controls (arch #254, 05#7): without these, items beyond the
          first page were unreachable. */}
      {knowledgeStatus === "loaded" &&
        knowledgePagination &&
        knowledgePagination.total_pages > 1 && (
          <div
            className="mt-4 flex items-center justify-between text-sm text-muted-foreground"
            data-testid="knowledge-pagination"
          >
            <span data-testid="knowledge-pagination-status">
              Page {knowledgePagination.page} of {knowledgePagination.total_pages}
              {" · "}
              {knowledgePagination.total_count} items
            </span>
            <div className="flex gap-2">
              <Button
                variant="outline"
                size="sm"
                data-testid="knowledge-page-prev"
                disabled={!knowledgePagination.has_previous}
                onClick={() => setPage((p) => Math.max(1, p - 1))}
              >
                Previous
              </Button>
              <Button
                variant="outline"
                size="sm"
                data-testid="knowledge-page-next"
                disabled={!knowledgePagination.has_next}
                onClick={() => setPage((p) => p + 1)}
              >
                Next
              </Button>
            </div>
          </div>
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
