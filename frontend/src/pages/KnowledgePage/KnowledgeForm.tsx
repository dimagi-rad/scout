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
import { Badge } from "@/components/ui/badge"
import type { KnowledgeItem, KnowledgeType } from "@/store/knowledgeSlice"

interface KnowledgeFormProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  item: KnowledgeItem | null
  onSave: (data: Partial<KnowledgeItem> & { type: KnowledgeType }) => Promise<void>
}

interface FormState {
  type: KnowledgeType
  name: string
  description: string
  sql_template: string
  rule_text: string
  context: string
  sql: string
  tags: string
  related_tables: string
}

const initialFormState: FormState = {
  type: "metric",
  name: "",
  description: "",
  sql_template: "",
  rule_text: "",
  context: "",
  sql: "",
  tags: "",
  related_tables: "",
}

const typeLabels: Record<KnowledgeType, string> = {
  metric: "Metric",
  rule: "Rule",
  query: "Query",
  learning: "Learning",
}

export function KnowledgeForm({ open, onOpenChange, item, onSave }: KnowledgeFormProps) {
  const [form, setForm] = useState<FormState>(initialFormState)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const isEdit = !!item
  const isLearning = form.type === "learning" || item?.type === "learning"

  useEffect(() => {
    if (item) {
      setForm({
        type: item.type,
        name: item.name || "",
        description: item.description || "",
        sql_template: item.sql_template || "",
        rule_text: item.rule_text || "",
        context: item.context || "",
        sql: item.sql || "",
        tags: item.tags?.join(", ") || "",
        related_tables: item.related_tables?.join(", ") || "",
      })
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

  const handleTypeChange = (value: string) => {
    setForm((prev) => ({ ...prev, type: value as KnowledgeType }))
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
      const data: Partial<KnowledgeItem> & { type: KnowledgeType } = {
        type: form.type,
        related_tables: parseCommaSeparated(form.related_tables),
      }

      // Add type-specific fields
      if (form.type === "metric") {
        data.name = form.name
        data.description = form.description || undefined
        data.sql_template = form.sql_template
      } else if (form.type === "rule") {
        data.name = form.name
        data.rule_text = form.rule_text
        data.context = form.context || undefined
      } else if (form.type === "query") {
        data.name = form.name
        data.description = form.description || undefined
        data.sql = form.sql
        data.tags = parseCommaSeparated(form.tags)
      }

      await onSave(data)
      onOpenChange(false)
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to save knowledge item")
    } finally {
      setLoading(false)
    }
  }

  // Learning items are view-only
  if (isLearning && item) {
    return (
      <Dialog open={open} onOpenChange={onOpenChange}>
        <DialogContent className="max-w-2xl">
          <DialogHeader>
            <DialogTitle>Learning Details</DialogTitle>
            <DialogDescription>
              This learning was automatically captured. You can promote it to a rule or query.
            </DialogDescription>
          </DialogHeader>

          <div className="space-y-4">
            {item.correction && (
              <div className="space-y-2">
                <Label>Correction</Label>
                <div className="rounded-md border p-3 bg-muted/50 text-sm">
                  {item.correction}
                </div>
              </div>
            )}

            {item.context && (
              <div className="space-y-2">
                <Label>Context</Label>
                <div className="rounded-md border p-3 bg-muted/50 text-sm">
                  {item.context}
                </div>
              </div>
            )}

            {item.confidence !== undefined && (
              <div className="space-y-2">
                <Label>Confidence</Label>
                <div className="flex items-center gap-2">
                  <div className="h-2 flex-1 rounded-full bg-muted">
                    <div
                      className="h-2 rounded-full bg-primary"
                      style={{ width: `${item.confidence * 100}%` }}
                    />
                  </div>
                  <span className="text-sm font-medium">
                    {Math.round(item.confidence * 100)}%
                  </span>
                </div>
              </div>
            )}

            {item.related_tables && item.related_tables.length > 0 && (
              <div className="space-y-2">
                <Label>Related Tables</Label>
                <div className="flex flex-wrap gap-1">
                  {item.related_tables.map((table) => (
                    <Badge key={table} variant="outline">
                      {table}
                    </Badge>
                  ))}
                </div>
              </div>
            )}

            {item.promoted_to && (
              <div className="rounded-md border border-green-200 bg-green-50 p-3 dark:border-green-800 dark:bg-green-900/20">
                <p className="text-sm text-green-800 dark:text-green-400">
                  This learning has been promoted to: <strong>{item.promoted_to}</strong>
                </p>
              </div>
            )}
          </div>

          <DialogFooter>
            <Button variant="outline" onClick={() => onOpenChange(false)}>
              Close
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    )
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>
            {isEdit ? `Edit ${typeLabels[form.type]}` : "New Knowledge Item"}
          </DialogTitle>
          <DialogDescription>
            {isEdit
              ? "Update the knowledge item details"
              : "Add a new metric, rule, or query to your knowledge base"}
          </DialogDescription>
        </DialogHeader>

        <form onSubmit={handleSubmit}>
          {error && (
            <div className="mb-4 rounded-md bg-destructive/10 p-3 text-sm text-destructive">
              {error}
            </div>
          )}

          <div className="space-y-4">
            {/* Type selector (only for create) */}
            {!isEdit && (
              <div className="space-y-2">
                <Label htmlFor="type">Type</Label>
                <Select value={form.type} onValueChange={handleTypeChange}>
                  <SelectTrigger className="w-full">
                    <SelectValue placeholder="Select type" />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="metric">Metric</SelectItem>
                    <SelectItem value="rule">Rule</SelectItem>
                    <SelectItem value="query">Query</SelectItem>
                  </SelectContent>
                </Select>
              </div>
            )}

            {/* Name field (for all types) */}
            <div className="space-y-2">
              <Label htmlFor="name">Name</Label>
              <Input
                id="name"
                name="name"
                value={form.name}
                onChange={handleChange}
                placeholder={`Enter ${form.type} name`}
                required
              />
            </div>

            {/* Metric-specific fields */}
            {form.type === "metric" && (
              <>
                <div className="space-y-2">
                  <Label htmlFor="description">Description</Label>
                  <Textarea
                    id="description"
                    name="description"
                    value={form.description}
                    onChange={handleChange}
                    placeholder="Describe what this metric measures"
                    rows={2}
                  />
                </div>
                <div className="space-y-2">
                  <Label htmlFor="sql_template">SQL Template</Label>
                  <Textarea
                    id="sql_template"
                    name="sql_template"
                    value={form.sql_template}
                    onChange={handleChange}
                    placeholder="SELECT COUNT(*) FROM orders WHERE ..."
                    rows={4}
                    className="font-mono text-sm"
                    required
                  />
                  <p className="text-xs text-muted-foreground">
                    Use placeholders like {"{start_date}"} for dynamic values
                  </p>
                </div>
              </>
            )}

            {/* Rule-specific fields */}
            {form.type === "rule" && (
              <>
                <div className="space-y-2">
                  <Label htmlFor="rule_text">Rule Text</Label>
                  <Textarea
                    id="rule_text"
                    name="rule_text"
                    value={form.rule_text}
                    onChange={handleChange}
                    placeholder="Always use LEFT JOIN when joining with optional tables..."
                    rows={3}
                    required
                  />
                </div>
                <div className="space-y-2">
                  <Label htmlFor="context">Context</Label>
                  <Textarea
                    id="context"
                    name="context"
                    value={form.context}
                    onChange={handleChange}
                    placeholder="When this rule should be applied"
                    rows={2}
                  />
                </div>
              </>
            )}

            {/* Query-specific fields */}
            {form.type === "query" && (
              <>
                <div className="space-y-2">
                  <Label htmlFor="description">Description</Label>
                  <Textarea
                    id="description"
                    name="description"
                    value={form.description}
                    onChange={handleChange}
                    placeholder="What this query does"
                    rows={2}
                  />
                </div>
                <div className="space-y-2">
                  <Label htmlFor="sql">SQL</Label>
                  <Textarea
                    id="sql"
                    name="sql"
                    value={form.sql}
                    onChange={handleChange}
                    placeholder="SELECT * FROM ..."
                    rows={4}
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
                    placeholder="reporting, sales, monthly"
                  />
                  <p className="text-xs text-muted-foreground">
                    Comma-separated list of tags
                  </p>
                </div>
              </>
            )}

            {/* Related tables (for all types) */}
            <div className="space-y-2">
              <Label htmlFor="related_tables">Related Tables</Label>
              <Input
                id="related_tables"
                name="related_tables"
                value={form.related_tables}
                onChange={handleChange}
                placeholder="users, orders, products"
              />
              <p className="text-xs text-muted-foreground">
                Comma-separated list of related database tables
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

interface PromoteDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  item: KnowledgeItem | null
  onPromote: (data: { target_type: "rule" | "query"; name: string; [key: string]: unknown }) => Promise<void>
}

export function PromoteDialog({ open, onOpenChange, item, onPromote }: PromoteDialogProps) {
  const [targetType, setTargetType] = useState<"rule" | "query">("rule")
  const [name, setName] = useState("")
  const [ruleText, setRuleText] = useState("")
  const [sql, setSql] = useState("")
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (item) {
      setName("")
      setRuleText(item.correction || "")
      setSql("")
      setTargetType("rule")
    }
    setError(null)
  }, [item, open])

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setLoading(true)
    setError(null)

    try {
      const data: { target_type: "rule" | "query"; name: string; [key: string]: unknown } = {
        target_type: targetType,
        name,
      }

      if (targetType === "rule") {
        data.rule_text = ruleText
      } else {
        data.sql = sql
      }

      await onPromote(data)
      onOpenChange(false)
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to promote learning")
    } finally {
      setLoading(false)
    }
  }

  if (!item) return null

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>Promote Learning</DialogTitle>
          <DialogDescription>
            Convert this learning into a permanent rule or query
          </DialogDescription>
        </DialogHeader>

        <form onSubmit={handleSubmit}>
          {error && (
            <div className="mb-4 rounded-md bg-destructive/10 p-3 text-sm text-destructive">
              {error}
            </div>
          )}

          {/* Original learning content */}
          {item.correction && (
            <div className="mb-4 rounded-md border bg-muted/50 p-3">
              <Label className="text-xs text-muted-foreground">Original Learning</Label>
              <p className="mt-1 text-sm">{item.correction}</p>
            </div>
          )}

          <div className="space-y-4">
            <div className="space-y-2">
              <Label htmlFor="target_type">Promote To</Label>
              <Select value={targetType} onValueChange={(v) => setTargetType(v as "rule" | "query")}>
                <SelectTrigger className="w-full">
                  <SelectValue placeholder="Select type" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="rule">Rule</SelectItem>
                  <SelectItem value="query">Query</SelectItem>
                </SelectContent>
              </Select>
            </div>

            <div className="space-y-2">
              <Label htmlFor="name">Name</Label>
              <Input
                id="name"
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder={`Enter ${targetType} name`}
                required
              />
            </div>

            {targetType === "rule" ? (
              <div className="space-y-2">
                <Label htmlFor="rule_text">Rule Text</Label>
                <Textarea
                  id="rule_text"
                  value={ruleText}
                  onChange={(e) => setRuleText(e.target.value)}
                  placeholder="Enter the rule text"
                  rows={4}
                  required
                />
              </div>
            ) : (
              <div className="space-y-2">
                <Label htmlFor="sql">SQL</Label>
                <Textarea
                  id="sql"
                  value={sql}
                  onChange={(e) => setSql(e.target.value)}
                  placeholder="Enter the SQL query"
                  rows={4}
                  className="font-mono text-sm"
                  required
                />
              </div>
            )}
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
              Promote to {targetType === "rule" ? "Rule" : "Query"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
