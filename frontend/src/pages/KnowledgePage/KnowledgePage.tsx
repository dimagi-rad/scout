import { useEffect, useState, useCallback } from "react"
import { useNavigate, useParams, useLocation } from "react-router-dom"
import { Plus } from "lucide-react"
import { useAppStore } from "@/store/store"
import { Button } from "@/components/ui/button"
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog"
import { KnowledgeList } from "./KnowledgeList"
import { KnowledgeForm, PromoteDialog } from "./KnowledgeForm"
import type { KnowledgeItem, KnowledgeType } from "@/store/knowledgeSlice"

export function KnowledgePage() {
  const { id } = useParams<{ id: string }>()
  const location = useLocation()
  const navigate = useNavigate()

  const activeProjectId = useAppStore((s) => s.activeProjectId)
  const knowledgeItems = useAppStore((s) => s.knowledgeItems)
  const knowledgeStatus = useAppStore((s) => s.knowledgeStatus)
  const knowledgeFilter = useAppStore((s) => s.knowledgeFilter)
  const knowledgeSearch = useAppStore((s) => s.knowledgeSearch)
  const {
    fetchKnowledge,
    createKnowledge,
    updateKnowledge,
    deleteKnowledge,
    promoteKnowledge,
    setFilter,
    setSearch,
  } = useAppStore((s) => s.knowledgeActions)

  const [formOpen, setFormOpen] = useState(false)
  const [editItem, setEditItem] = useState<KnowledgeItem | null>(null)
  const [deleteItem, setDeleteItem] = useState<KnowledgeItem | null>(null)
  const [promoteItem, setPromoteItem] = useState<KnowledgeItem | null>(null)

  // Check if we're on /knowledge/new
  const isNew = location.pathname.endsWith("/new")

  // Fetch knowledge on mount and when project/filter/search changes
  useEffect(() => {
    if (activeProjectId) {
      fetchKnowledge(activeProjectId, knowledgeFilter ?? undefined, knowledgeSearch || undefined)
    }
  }, [activeProjectId, knowledgeFilter, knowledgeSearch, fetchKnowledge])

  // Open form for new item
  useEffect(() => {
    if (isNew) {
      setEditItem(null)
      setFormOpen(true)
    }
  }, [isNew])

  // Open form for editing specific item
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

  const handlePromote = (item: KnowledgeItem) => {
    setPromoteItem(item)
  }

  const handleFormClose = (open: boolean) => {
    setFormOpen(open)
    if (!open) {
      setEditItem(null)
      // Navigate back to list if we were on /new or /:id
      if (isNew || id) {
        navigate("/knowledge")
      }
    }
  }

  const handleSave = async (data: Partial<KnowledgeItem> & { type: KnowledgeType }) => {
    if (!activeProjectId) return

    if (editItem) {
      await updateKnowledge(activeProjectId, editItem.id, data)
    } else {
      await createKnowledge(activeProjectId, data)
    }
  }

  const handleConfirmDelete = async () => {
    if (!activeProjectId || !deleteItem) return

    await deleteKnowledge(activeProjectId, deleteItem.id)
    setDeleteItem(null)
  }

  const handleConfirmPromote = async (data: { target_type: "rule" | "query"; name: string; [key: string]: unknown }) => {
    if (!activeProjectId || !promoteItem) return

    await promoteKnowledge(activeProjectId, promoteItem.id, data)
    setPromoteItem(null)
  }

  // Filter items locally based on current filter and search
  const filteredItems = knowledgeItems.filter((item) => {
    if (knowledgeFilter && item.type !== knowledgeFilter) return false
    if (knowledgeSearch) {
      const search = knowledgeSearch.toLowerCase()
      const searchableText = [
        item.name,
        item.description,
        item.rule_text,
        item.sql_template,
        item.sql,
        item.correction,
        ...(item.tags || []),
        ...(item.related_tables || []),
      ]
        .filter(Boolean)
        .join(" ")
        .toLowerCase()
      return searchableText.includes(search)
    }
    return true
  })

  if (!activeProjectId) {
    return (
      <div className="container mx-auto py-8">
        <div className="rounded-lg border border-dashed p-8 text-center">
          <p className="text-muted-foreground">Please select a project first</p>
        </div>
      </div>
    )
  }

  return (
    <div className="container mx-auto py-8">
      {/* Header */}
      <div className="mb-8 flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold">Knowledge Base</h1>
          <p className="text-muted-foreground">
            Manage metrics, rules, queries, and learnings
          </p>
        </div>
        <Button onClick={handleNewClick}>
          <Plus className="mr-2 h-4 w-4" />
          New
        </Button>
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
          onPromote={handlePromote}
        />
      )}

      {/* Create/Edit Form Dialog */}
      <KnowledgeForm
        open={formOpen}
        onOpenChange={handleFormClose}
        item={editItem}
        onSave={handleSave}
      />

      {/* Promote Dialog */}
      <PromoteDialog
        open={!!promoteItem}
        onOpenChange={(open) => !open && setPromoteItem(null)}
        item={promoteItem}
        onPromote={handleConfirmPromote}
      />

      {/* Delete Confirmation Dialog */}
      <AlertDialog open={!!deleteItem} onOpenChange={() => setDeleteItem(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete Knowledge Item</AlertDialogTitle>
            <AlertDialogDescription>
              Are you sure you want to delete this {deleteItem?.type}? This action
              cannot be undone.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction onClick={handleConfirmDelete}>Delete</AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}
