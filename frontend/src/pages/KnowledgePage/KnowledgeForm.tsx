import { useState, useEffect } from "react"
import { Loader2 } from "lucide-react"
import { useIsCurrentAccount } from "@/hooks/useIsCurrentAccount"
import { READ_ONLY_HINT, writeErrorMessage } from "@/hooks/useWorkspaceRole"
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
import type { KnowledgeItem, KnowledgeType } from "@/store/knowledgeSlice"

interface KnowledgeFormProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  item: KnowledgeItem | null
  onSave: (data: Partial<KnowledgeItem> & { type: KnowledgeType }) => Promise<void>
  readOnly?: boolean
}

interface FormState {
  type: KnowledgeType
  title: string
  content: string
  tags: string
}

const initialFormState: FormState = {
  type: "entry",
  title: "",
  content: "",
  tags: "",
}

export function KnowledgeForm({
  open,
  onOpenChange,
  item,
  onSave,
  readOnly = false,
}: KnowledgeFormProps) {
  const isCurrentAccount = useIsCurrentAccount()
  const [form, setForm] = useState<FormState>(initialFormState)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const isEdit = !!item

  useEffect(() => {
    if (item) {
      const formData: FormState = { ...initialFormState, type: item.type }

      if (item.type === "entry") {
        formData.title = item.title || ""
        formData.content = item.content || ""
        formData.tags = item.tags?.join(", ") || ""
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
    if (readOnly) return
    setLoading(true)
    setError(null)

    try {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const data: any = { type: form.type }

      if (form.type === "entry") {
        data.title = form.title
        data.content = form.content
        data.tags = parseCommaSeparated(form.tags)
      }

      await onSave(data)
      if (!isCurrentAccount()) return
      onOpenChange(false)
    } catch (err) {
      if (!isCurrentAccount()) return
      const fallback = err instanceof Error ? err.message : "Failed to save knowledge item"
      setError(writeErrorMessage(err, fallback, !readOnly))
    } finally {
      if (isCurrentAccount()) setLoading(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>
            {readOnly ? "View Entry" : isEdit ? "Edit Entry" : "New Knowledge Entry"}
          </DialogTitle>
          <DialogDescription>
            {readOnly
              ? READ_ONLY_HINT
              : isEdit
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
                readOnly={readOnly}
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
                readOnly={readOnly}
                value={form.content}
                onChange={handleChange}
                placeholder="Markdown content (metric definitions, semantic-model notes, business rules, etc.)"
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
                readOnly={readOnly}
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
              {readOnly ? "Close" : "Cancel"}
            </Button>
            {!readOnly && (
              <Button type="submit" disabled={loading}>
                {loading && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
                {isEdit ? "Save Changes" : "Create"}
              </Button>
            )}
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
