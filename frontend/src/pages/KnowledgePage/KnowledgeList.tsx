import { Search, Pencil, Trash2, ArrowUpCircle } from "lucide-react"
import { Input } from "@/components/ui/input"
import { Button } from "@/components/ui/button"
import { Badge } from "@/components/ui/badge"
import { Card, CardContent, CardHeader } from "@/components/ui/card"
import type { KnowledgeItem, KnowledgeType } from "@/store/knowledgeSlice"

interface KnowledgeListProps {
  items: KnowledgeItem[]
  filter: KnowledgeType | null
  search: string
  onFilterChange: (type: KnowledgeType | null) => void
  onSearchChange: (search: string) => void
  onEdit: (item: KnowledgeItem) => void
  onDelete: (item: KnowledgeItem) => void
  onPromote: (item: KnowledgeItem) => void
}

const typeFilters: { value: KnowledgeType | null; label: string }[] = [
  { value: null, label: "All" },
  { value: "metric", label: "Metrics" },
  { value: "rule", label: "Rules" },
  { value: "query", label: "Queries" },
  { value: "learning", label: "Learnings" },
]

const typeBadgeStyles: Record<KnowledgeType, string> = {
  metric: "bg-blue-100 text-blue-800 dark:bg-blue-900/30 dark:text-blue-400",
  rule: "bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-400",
  query: "bg-purple-100 text-purple-800 dark:bg-purple-900/30 dark:text-purple-400",
  learning: "bg-yellow-100 text-yellow-800 dark:bg-yellow-900/30 dark:text-yellow-400",
}

function getItemTitle(item: KnowledgeItem): string {
  if (item.name) return item.name
  if (item.description) return item.description.slice(0, 50) + (item.description.length > 50 ? "..." : "")
  if (item.rule_text) return item.rule_text.slice(0, 50) + (item.rule_text.length > 50 ? "..." : "")
  if (item.correction) return item.correction.slice(0, 50) + (item.correction.length > 50 ? "..." : "")
  return `${item.type} item`
}

function getItemDescription(item: KnowledgeItem): string | null {
  if (item.type === "metric") {
    return item.sql_template ? `SQL: ${item.sql_template.slice(0, 100)}...` : null
  }
  if (item.type === "rule") {
    return item.rule_text || null
  }
  if (item.type === "query") {
    return item.sql ? `SQL: ${item.sql.slice(0, 100)}...` : item.description || null
  }
  if (item.type === "learning") {
    return item.correction || null
  }
  return null
}

export function KnowledgeList({
  items,
  filter,
  search,
  onFilterChange,
  onSearchChange,
  onEdit,
  onDelete,
  onPromote,
}: KnowledgeListProps) {
  return (
    <div className="space-y-4">
      {/* Search and Filters */}
      <div className="flex flex-col gap-4 sm:flex-row sm:items-center">
        <div className="relative flex-1">
          <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            placeholder="Search knowledge..."
            value={search}
            onChange={(e) => onSearchChange(e.target.value)}
            className="pl-9"
          />
        </div>
        <div className="flex flex-wrap gap-2">
          {typeFilters.map((t) => (
            <Button
              key={t.value ?? "all"}
              variant={filter === t.value ? "default" : "outline"}
              size="sm"
              onClick={() => onFilterChange(t.value)}
            >
              {t.label}
            </Button>
          ))}
        </div>
      </div>

      {/* Knowledge Items */}
      {items.length === 0 ? (
        <div className="rounded-lg border border-dashed p-8 text-center">
          <p className="text-muted-foreground">No knowledge items found</p>
        </div>
      ) : (
        <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
          {items.map((item) => (
            <Card key={item.id} className="flex flex-col">
              <CardHeader className="pb-2">
                <div className="flex items-start justify-between gap-2">
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2 mb-1">
                      <Badge
                        variant="secondary"
                        className={typeBadgeStyles[item.type]}
                      >
                        {item.type}
                      </Badge>
                      {item.type === "learning" && item.confidence !== undefined && (
                        <span className="text-xs text-muted-foreground">
                          {Math.round(item.confidence * 100)}% confidence
                        </span>
                      )}
                    </div>
                    <h3 className="font-medium truncate" title={getItemTitle(item)}>
                      {getItemTitle(item)}
                    </h3>
                  </div>
                </div>
              </CardHeader>
              <CardContent className="flex-1 flex flex-col">
                {getItemDescription(item) && (
                  <p className="text-sm text-muted-foreground line-clamp-2 mb-3">
                    {getItemDescription(item)}
                  </p>
                )}

                {/* Related Tables */}
                {item.related_tables && item.related_tables.length > 0 && (
                  <div className="flex flex-wrap gap-1 mb-3">
                    {item.related_tables.slice(0, 3).map((table) => (
                      <Badge key={table} variant="outline" className="text-xs">
                        {table}
                      </Badge>
                    ))}
                    {item.related_tables.length > 3 && (
                      <Badge variant="outline" className="text-xs">
                        +{item.related_tables.length - 3}
                      </Badge>
                    )}
                  </div>
                )}

                {/* Tags for queries */}
                {item.type === "query" && item.tags && item.tags.length > 0 && (
                  <div className="flex flex-wrap gap-1 mb-3">
                    {item.tags.slice(0, 3).map((tag) => (
                      <Badge key={tag} variant="secondary" className="text-xs">
                        {tag}
                      </Badge>
                    ))}
                    {item.tags.length > 3 && (
                      <Badge variant="secondary" className="text-xs">
                        +{item.tags.length - 3}
                      </Badge>
                    )}
                  </div>
                )}

                {/* Promoted indicator for learnings */}
                {item.type === "learning" && item.promoted_to && (
                  <p className="text-xs text-green-600 dark:text-green-400 mb-3">
                    Promoted to {item.promoted_to}
                  </p>
                )}

                {/* Actions */}
                <div className="mt-auto flex items-center gap-2 pt-2 border-t">
                  {item.type === "learning" && !item.promoted_to ? (
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() => onPromote(item)}
                      className="flex-1"
                    >
                      <ArrowUpCircle className="mr-1 h-4 w-4" />
                      Promote
                    </Button>
                  ) : item.type !== "learning" ? (
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => onEdit(item)}
                    >
                      <Pencil className="mr-1 h-4 w-4" />
                      Edit
                    </Button>
                  ) : null}
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => onDelete(item)}
                    className="text-destructive hover:text-destructive"
                  >
                    <Trash2 className="mr-1 h-4 w-4" />
                    Delete
                  </Button>
                </div>
              </CardContent>
            </Card>
          ))}
        </div>
      )}
    </div>
  )
}
