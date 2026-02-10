import { Play, Pencil, Trash2, Clock, Hash, Variable } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader } from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import type { Recipe } from "@/store/recipeSlice"

interface RecipesListProps {
  recipes: Recipe[]
  onView: (recipe: Recipe) => void
  onRun: (recipe: Recipe) => void
  onDelete: (recipe: Recipe) => void
}

function formatDate(dateString: string | undefined): string {
  if (!dateString) return "Never"
  const date = new Date(dateString)
  return date.toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
  })
}

export function RecipesList({ recipes, onView, onRun, onDelete }: RecipesListProps) {
  if (recipes.length === 0) {
    return (
      <div className="rounded-lg border border-dashed p-8 text-center">
        <p className="text-muted-foreground">No recipes found</p>
        <p className="mt-2 text-sm text-muted-foreground">
          Recipes are created by the AI agent during chat conversations.
        </p>
      </div>
    )
  }

  return (
    <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
      {recipes.map((recipe) => (
        <Card key={recipe.id} className="flex flex-col">
          <CardHeader className="pb-2">
            <div className="flex items-start justify-between gap-2">
              <div className="flex-1 min-w-0">
                <h3 className="font-medium truncate" title={recipe.name}>
                  {recipe.name}
                </h3>
                {recipe.description && (
                  <p className="text-sm text-muted-foreground line-clamp-2 mt-1">
                    {recipe.description}
                  </p>
                )}
              </div>
            </div>
          </CardHeader>
          <CardContent className="flex-1 flex flex-col">
            {/* Stats */}
            <div className="flex flex-wrap gap-3 mb-4">
              <div className="flex items-center gap-1 text-sm text-muted-foreground">
                <Hash className="h-4 w-4" />
                <span>{recipe.step_count ?? recipe.steps?.length ?? 0} steps</span>
              </div>
              <div className="flex items-center gap-1 text-sm text-muted-foreground">
                <Variable className="h-4 w-4" />
                <span>{recipe.variable_count ?? recipe.variables?.length ?? 0} variables</span>
              </div>
            </div>

            {/* Variables preview */}
            {recipe.variables && recipe.variables.length > 0 && (
              <div className="flex flex-wrap gap-1 mb-3">
                {recipe.variables.slice(0, 3).map((v) => (
                  <Badge key={v.name} variant="outline" className="text-xs">
                    {v.name}
                    {v.required && <span className="text-destructive ml-0.5">*</span>}
                  </Badge>
                ))}
                {recipe.variables.length > 3 && (
                  <Badge variant="outline" className="text-xs">
                    +{recipe.variables.length - 3} more
                  </Badge>
                )}
              </div>
            )}

            {/* Last run */}
            <div className="flex items-center gap-1 text-xs text-muted-foreground mb-3">
              <Clock className="h-3 w-3" />
              <span>Last run: {formatDate(recipe.last_run_at)}</span>
            </div>

            {/* Actions */}
            <div className="mt-auto flex items-center gap-2 pt-2 border-t">
              <Button
                variant="default"
                size="sm"
                onClick={() => onRun(recipe)}
                className="flex-1"
              >
                <Play className="mr-1 h-4 w-4" />
                Run
              </Button>
              <Button
                variant="ghost"
                size="sm"
                onClick={() => onView(recipe)}
              >
                <Pencil className="mr-1 h-4 w-4" />
                Edit
              </Button>
              <Button
                variant="ghost"
                size="sm"
                onClick={() => onDelete(recipe)}
                className="text-destructive hover:text-destructive"
              >
                <Trash2 className="h-4 w-4" />
              </Button>
            </div>
          </CardContent>
        </Card>
      ))}
    </div>
  )
}
