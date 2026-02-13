import { Search, Pencil, Trash2 } from "lucide-react"
import { Input } from "@/components/ui/input"
import { Button } from "@/components/ui/button"
import { Badge } from "@/components/ui/badge"
import { Card, CardContent, CardHeader } from "@/components/ui/card"
import {
  getKnowledgeItemName,
  type KnowledgeItem,
  type KnowledgeType,
  type LearningItem,
} from "@/store/knowledgeSlice"

interface KnowledgeListProps {
  items: KnowledgeItem[]
  filter: KnowledgeType | null
  search: string
  onFilterChange: (type: KnowledgeType | null) => void
  onSearchChange: (search: string) => void
  onEdit: (item: KnowledgeItem) => void
  onDelete: (item: KnowledgeItem) => void
}

const typeFilters: { value: KnowledgeType | null; label: string }[] = [
  { value: null, label: "All" },
  { value: "entry", label: "Entries" },
  { value: "learning", label: "Learnings" },
]

const typeBadgeStyles: Record<KnowledgeType, string> = {
  entry: "bg-blue-100 text-blue-800 dark:bg-blue-900/30 dark:text-blue-400",
  learning: "bg-yellow-100 text-yellow-800 dark:bg-yellow-900/30 dark:text-yellow-400",
}

function getItemDescription(item: KnowledgeItem): string | null {
  switch (item.type) {
    case "entry":
      return item.content ? item.content.slice(0, 120) + (item.content.length > 120 ? "..." : "") : null
    case "learning":
      return item.corrected_sql ? `Corrected: ${item.corrected_sql.slice(0, 100)}...` : null
  }
}

function getRelatedTables(item: KnowledgeItem): string[] {
  if (item.type === "learning") {
    return item.applies_to_tables || []
  }
  return []
}

export function KnowledgeList({
  items,
  filter,
  search,
  onFilterChange,
  onSearchChange,
  onEdit,
  onDelete,
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
          {items.map((item) => {
            const title = getKnowledgeItemName(item)
            const relatedTables = getRelatedTables(item)
            const isLearning = item.type === "learning"
            const learningItem = isLearning ? (item as LearningItem) : null

            return (
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
                      {learningItem?.confidence_score !== undefined && (
                        <span className="text-xs text-muted-foreground">
                          {Math.round(learningItem.confidence_score * 100)}% confidence
                        </span>
                      )}
                    </div>
                    <h3 className="font-medium truncate" title={title}>
                      {title}
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
                {relatedTables.length > 0 && (
                  <div className="flex flex-wrap gap-1 mb-3">
                    {relatedTables.slice(0, 3).map((table) => (
                      <Badge key={table} variant="outline" className="text-xs">
                        {table}
                      </Badge>
                    ))}
                    {relatedTables.length > 3 && (
                      <Badge variant="outline" className="text-xs">
                        +{relatedTables.length - 3}
                      </Badge>
                    )}
                  </div>
                )}

                {/* Tags for entries */}
                {item.type === "entry" && item.tags && item.tags.length > 0 && (
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

                {/* Actions */}
                <div className="mt-auto flex items-center gap-2 pt-2 border-t">
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => onEdit(item)}
                  >
                    <Pencil className="mr-1 h-4 w-4" />
                    Edit
                  </Button>
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
            )
          })}
        </div>
      )}
    </div>
  )
}
