import { useState, useEffect } from "react"
import { Loader2 } from "lucide-react"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Textarea } from "@/components/ui/textarea"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import type { KnowledgeItem, KnowledgeType, LearningItem } from "@/store/knowledgeSlice"

interface KnowledgeFormProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  item: KnowledgeItem | null
  onSave: (data: Partial<KnowledgeItem> & { type: KnowledgeType }) => Promise<void>
}

interface FormState {
  type: KnowledgeType
  // Entry fields
  title: string
  content: string
  tags: string
  // Learning editable fields
  description: string
  category: string
  applies_to_tables: string
}

const initialFormState: FormState = {
  type: "entry",
  title: "",
  content: "",
  tags: "",
  description: "",
  category: "other",
  applies_to_tables: "",
}

const categoryOptions = [
  { value: "type_mismatch", label: "Column type mismatch" },
  { value: "filter_required", label: "Missing required filter" },
  { value: "join_pattern", label: "Correct join pattern" },
  { value: "aggregation", label: "Aggregation gotcha" },
  { value: "naming", label: "Column/table naming convention" },
  { value: "data_quality", label: "Data quality issue" },
  { value: "business_logic", label: "Business logic correction" },
  { value: "other", label: "Other" },
]

export function KnowledgeForm({ open, onOpenChange, item, onSave }: KnowledgeFormProps) {
  const [form, setForm] = useState<FormState>(initialFormState)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const isEdit = !!item
  const isLearning = item?.type === "learning"

  useEffect(() => {
    if (item) {
      const formData: FormState = { ...initialFormState, type: item.type }

      if (item.type === "entry") {
        formData.title = item.title || ""
        formData.content = item.content || ""
        formData.tags = item.tags?.join(", ") || ""
      } else if (item.type === "learning") {
        formData.description = item.description || ""
        formData.category = item.category || "other"
        formData.applies_to_tables = item.applies_to_tables?.join(", ") || ""
      }

      setForm(formData)
    } else {
      setForm(initialFormState)
    }
    setError(null)
  }, [item, open])

  const handleChange = (
    e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement>
  ) => {
    const { name, value } = e.target
    setForm((prev) => ({ ...prev, [name]: value }))
  }

  const parseCommaSeparated = (value: string): string[] => {
    return value
      .split(",")
      .map((s) => s.trim())
      .filter((s) => s.length > 0)
  }

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setLoading(true)
    setError(null)

    try {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const data: any = { type: form.type }

      if (form.type === "entry") {
        data.title = form.title
        data.content = form.content
        data.tags = parseCommaSeparated(form.tags)
      } else if (form.type === "learning") {
        data.description = form.description
        data.category = form.category
        data.applies_to_tables = parseCommaSeparated(form.applies_to_tables)
      }

      await onSave(data)
      onOpenChange(false)
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to save knowledge item")
    } finally {
      setLoading(false)
    }
  }

  // Learning edit form with read-only evidence
  if (isLearning && item && item.type === "learning") {
    const learningItem = item as LearningItem
    return (
      <Dialog open={open} onOpenChange={onOpenChange}>
        <DialogContent className="max-w-2xl">
          <DialogHeader>
            <DialogTitle>Edit Learning</DialogTitle>
            <DialogDescription>
              Edit the description, category, and tables for this learning.
            </DialogDescription>
          </DialogHeader>

          <form onSubmit={handleSubmit}>
            {error && (
              <div className="mb-4 rounded-md bg-destructive/10 p-3 text-sm text-destructive">
                {error}
              </div>
            )}

            <div className="space-y-4">
              <div className="space-y-2">
                <Label htmlFor="description">Description</Label>
                <Textarea
                  id="description"
                  name="description"
                  value={form.description}
                  onChange={handleChange}
                  rows={3}
                />
              </div>

              <div className="space-y-2">
                <Label htmlFor="category">Category</Label>
                <Select value={form.category} onValueChange={(v) => setForm((prev) => ({ ...prev, category: v }))}>
                  <SelectTrigger className="w-full">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {categoryOptions.map((opt) => (
                      <SelectItem key={opt.value} value={opt.value}>{opt.label}</SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>

              <div className="space-y-2">
                <Label htmlFor="applies_to_tables">Applies to Tables</Label>
                <Input
                  id="applies_to_tables"
                  name="applies_to_tables"
                  value={form.applies_to_tables}
                  onChange={handleChange}
                  placeholder="users, orders, products"
                />
              </div>

              {/* Read-only evidence */}
              {learningItem.original_error && (
                <div className="space-y-2">
                  <Label>Original Error</Label>
                  <div className="rounded-md border p-3 bg-muted/50 text-sm text-destructive">
                    {learningItem.original_error}
                  </div>
                </div>
              )}

              {learningItem.original_sql && (
                <div className="space-y-2">
                  <Label>Original SQL</Label>
                  <div className="rounded-md border p-3 bg-muted/50 text-sm font-mono">
                    {learningItem.original_sql}
                  </div>
                </div>
              )}

              {learningItem.corrected_sql && (
                <div className="space-y-2">
                  <Label>Corrected SQL</Label>
                  <div className="rounded-md border p-3 bg-muted/50 text-sm font-mono">
                    {learningItem.corrected_sql}
                  </div>
                </div>
              )}

              {learningItem.confidence_score !== undefined && (
                <div className="space-y-2">
                  <Label>Confidence</Label>
                  <div className="flex items-center gap-2">
                    <div className="h-2 flex-1 rounded-full bg-muted">
                      <div
                        className="h-2 rounded-full bg-primary"
                        style={{ width: `${learningItem.confidence_score * 100}%` }}
                      />
                    </div>
                    <span className="text-sm font-medium">
                      {Math.round(learningItem.confidence_score * 100)}%
                    </span>
                  </div>
                </div>
              )}
            </div>

            <DialogFooter className="mt-6">
              <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
                Cancel
              </Button>
              <Button type="submit" disabled={loading}>
                {loading && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
                Save Changes
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
    )
  }

  // Entry create/edit form
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>
            {isEdit ? "Edit Entry" : "New Knowledge Entry"}
          </DialogTitle>
          <DialogDescription>
            {isEdit
              ? "Update the knowledge entry details"
              : "Add a new entry to your knowledge base"}
          </DialogDescription>
        </DialogHeader>

        <form onSubmit={handleSubmit}>
          {error && (
            <div className="mb-4 rounded-md bg-destructive/10 p-3 text-sm text-destructive">
              {error}
            </div>
          )}

          <div className="space-y-4">
            <div className="space-y-2">
              <Label htmlFor="title">Title</Label>
              <Input
                id="title"
                name="title"
                value={form.title}
                onChange={handleChange}
                placeholder="Enter a title"
                required
              />
            </div>

            <div className="space-y-2">
              <Label htmlFor="content">Content</Label>
              <Textarea
                id="content"
                name="content"
                value={form.content}
                onChange={handleChange}
                placeholder="Markdown content (metric definitions, SQL snippets, business rules, etc.)"
                rows={10}
                className="font-mono text-sm"
                required
              />
            </div>

            <div className="space-y-2">
              <Label htmlFor="tags">Tags</Label>
              <Input
                id="tags"
                name="tags"
                value={form.tags}
                onChange={handleChange}
                placeholder="metric, finance, revenue"
              />
              <p className="text-xs text-muted-foreground">
                Comma-separated list of tags for categorization
              </p>
            </div>
          </div>

          <DialogFooter className="mt-6">
            <Button
              type="button"
              variant="outline"
              onClick={() => onOpenChange(false)}
            >
              Cancel
            </Button>
            <Button type="submit" disabled={loading}>
              {loading && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
              {isEdit ? "Save Changes" : "Create"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
