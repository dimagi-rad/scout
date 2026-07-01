import { Search } from "lucide-react"
import { useNavigate } from "react-router-dom"
import { ArtifactCard } from "@/components/ArtifactCard"
import { Input } from "@/components/ui/input"
import type { ArtifactSummary } from "@/store/artifactSlice"

interface ArtifactListProps {
  items: ArtifactSummary[]
  search: string
  onSearchChange: (search: string) => void
  onUpdate: (item: ArtifactSummary, data: { title?: string; description?: string }) => Promise<void>
  onDelete: (item: ArtifactSummary) => void
}

export function ArtifactList({ items, search, onSearchChange, onUpdate, onDelete }: ArtifactListProps) {
  const navigate = useNavigate()

  function handleOpen(artifact: ArtifactSummary) {
    navigate(`/artifacts/${artifact.id}`)
  }

  if (items.length === 0 && !search) {
    return (
      <div className="rounded-lg border border-dashed p-8 text-center">
        <p className="text-muted-foreground">
          No artifacts yet. Artifacts are created by the AI agent during chat conversations.
        </p>
      </div>
    )
  }

  return (
    <div className="space-y-4">
      {/* Search */}
      <div className="relative max-w-sm">
        <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
        <Input
          value={search}
          onChange={(e) => onSearchChange(e.target.value)}
          placeholder="Search artifacts..."
          className="pl-9"
          data-testid="artifact-search"
        />
      </div>

      {items.length === 0 && search && (
        <div className="rounded-lg border border-dashed p-8 text-center">
          <p className="text-muted-foreground">
            No artifacts match "{search}"
          </p>
        </div>
      )}

      {/* Card grid */}
      <div className="grid grid-cols-[repeat(auto-fill,minmax(min(100%,18rem),1fr))] gap-4">
        {items.map((item) => (
          <ArtifactCard
            key={item.id}
            artifact={item}
            onOpen={() => handleOpen(item)}
            onUpdate={(data) => onUpdate(item, data)}
            onDelete={() => onDelete(item)}
          />
        ))}
      </div>
    </div>
  )
}
