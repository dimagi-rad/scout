import { useState, useEffect } from "react"
import { Download, Loader2 } from "lucide-react"
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
import {
  REFERENCE_DESCRIPTION_MAX_LENGTH,
  type KnowledgeItem,
  type KnowledgeType,
} from "@/store/knowledgeSlice"
import { formatChars } from "./referenceFormat"

interface KnowledgeFormProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  item: KnowledgeItem | null
  onSave: (data: Partial<KnowledgeItem> & { type: KnowledgeType }) => Promise<void>
  readOnly?: boolean
  onDownload?: (item: KnowledgeItem) => Promise<void>
}

interface FormState {
  type: KnowledgeType
  title: string
  description: string
  content: string
  tags: string
}

const initialFormState: FormState = {
  type: "entry",
  title: "",
  description: "",
  content: "",
  tags: "",
}

export function KnowledgeForm({
  open,
  onOpenChange,
  item,
  onSave,
  readOnly = false,
  onDownload,
}: KnowledgeFormProps) {
  const isCurrentAccount = useIsCurrentAccount()
  const [form, setForm] = useState<FormState>(initialFormState)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [downloading, setDownloading] = useState(false)

  const isEdit = !!item
  // A reference document can be megabytes: show a preview and a download, never a textarea.
  const isReference = item?.kind === "reference"

  useEffect(() => {
    if (item) {
      const formData: FormState = { ...initialFormState, type: item.type }

      if (item.type === "entry") {
        formData.title = item.title || ""
        formData.description = item.description || ""
        formData.content = item.content || ""
        formData.tags = item.tags?.join(", ") || ""
      }

      setForm(formData)
    } else {
      setForm(initialFormState)
    }
    setError(null)
    setLoading(false)
    setDownloading(false)
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
        data.tags = parseCommaSeparated(form.tags)
        if (isReference) {
          data.description = form.description
        } else {
          data.content = form.content
        }
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

  const handleDownload = async () => {
    if (!item || !onDownload) return
    setDownloading(true)
    setError(null)
    try {
      await onDownload(item)
    } catch (err) {
      if (!isCurrentAccount()) return
      setError(err instanceof Error ? err.message : "Download failed. Try again.")
    } finally {
      if (isCurrentAccount()) setDownloading(false)
    }
  }

  const entryNoun = isReference ? "Reference Document" : "Entry"
  let dialogHint = "Add a new entry to your knowledge base"
  if (readOnly) dialogHint = READ_ONLY_HINT
  else if (isReference) dialogHint = "Update how Scout sees this document"
  else if (isEdit) dialogHint = "Update the knowledge entry details"

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[90vh] max-w-2xl overflow-y-auto">
        <DialogHeader>
          <DialogTitle>
            {readOnly ? `View ${entryNoun}` : isEdit ? `Edit ${entryNoun}` : "New Knowledge Entry"}
          </DialogTitle>
          <DialogDescription>{dialogHint}</DialogDescription>
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

            {isReference && item ? (
              <>
                <div className="space-y-2">
                  <Label htmlFor="description">Description</Label>
                  <Input
                    id="description"
                    name="description"
                    readOnly={readOnly}
                    value={form.description}
                    onChange={handleChange}
                    placeholder="What it is and when Scout should use it"
                    maxLength={REFERENCE_DESCRIPTION_MAX_LENGTH}
                    data-testid="knowledge-form-description"
                  />
                  <p className="text-xs text-muted-foreground">
                    Scout sees the title and this line in every chat, and reads the document
                    itself only when a question needs it.
                  </p>
                </div>
                <div className="space-y-2">
                  <div className="flex items-center justify-between">
                    <span id="reference-content-label" className="text-sm font-medium">
                      Content
                    </span>
                    {onDownload && (
                      <Button
                        type="button"
                        variant="outline"
                        size="sm"
                        onClick={handleDownload}
                        disabled={downloading}
                        data-testid="knowledge-form-download"
                      >
                        {downloading ? (
                          <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                        ) : (
                          <Download className="mr-2 h-4 w-4" />
                        )}
                        Download
                      </Button>
                    )}
                  </div>
                  <pre
                    aria-labelledby="reference-content-label"
                    className="max-h-40 overflow-auto whitespace-pre-wrap break-words rounded-md border bg-muted/40 p-3 font-mono text-xs"
                    data-testid="knowledge-form-preview"
                  >
                    {item.content}
                  </pre>
                  <p className="text-xs text-muted-foreground" data-testid="knowledge-form-preview-note">
                    {item.content_truncated
                      ? `Preview of the first ${item.content.length.toLocaleString()} of ${item.size.toLocaleString()} characters.`
                      : `${formatChars(item.size)}.`}{" "}
                    To change the content, upload a new version and delete this one.
                  </p>
                </div>
              </>
            ) : (
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
            )}

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
