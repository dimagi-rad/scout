import { useState, useEffect } from "react"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import { Skeleton } from "@/components/ui/skeleton"

interface RecipeVariable {
  name: string
  type: string
  required: boolean
  default?: string
  options?: string[]
}

interface RecipeStep {
  id: string
  order: number
  prompt_template: string
}

interface PublicRecipe {
  id: string
  name: string
  description: string
  variables: RecipeVariable[]
  steps: RecipeStep[]
  created_at: string
}

const variableTypeBadgeStyles: Record<string, string> = {
  string: "bg-blue-100 text-blue-800 dark:bg-blue-900/30 dark:text-blue-400",
  number: "bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-400",
  date: "bg-purple-100 text-purple-800 dark:bg-purple-900/30 dark:text-purple-400",
  boolean: "bg-yellow-100 text-yellow-800 dark:bg-yellow-900/30 dark:text-yellow-400",
  select: "bg-orange-100 text-orange-800 dark:bg-orange-900/30 dark:text-orange-400",
}

function formatDate(dateString: string): string {
  return new Date(dateString).toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
  })
}

function getTokenFromPath(): string | undefined {
  // /shared/recipes/{token}/ or /shared/recipes/{token}
  const match = window.location.pathname.match(/^\/shared\/recipes\/([^/]+)/)
  return match?.[1]
}

export function PublicRecipePage() {
  const token = getTokenFromPath()
  const [recipe, setRecipe] = useState<PublicRecipe | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    if (!token) return
    fetch(`/api/recipes/shared/${token}/`)
      .then((res) => {
        if (!res.ok) throw new Error(res.status === 404 ? "Recipe not found" : "Failed to load recipe")
        return res.json()
      })
      .then(setRecipe)
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false))
  }, [token])

  if (loading) {
    return (
      <div className="mx-auto max-w-3xl p-6 space-y-6">
        <Skeleton className="h-10 w-64" />
        <Skeleton className="h-4 w-48" />
        <Skeleton className="h-40 w-full" />
        <Skeleton className="h-40 w-full" />
      </div>
    )
  }

  if (error || !recipe) {
    return (
      <div className="flex min-h-screen items-center justify-center">
        <Card className="w-full max-w-md">
          <CardContent className="pt-6 text-center">
            <p className="text-lg font-medium text-destructive">{error ?? "Recipe not found"}</p>
            <p className="mt-2 text-sm text-muted-foreground">
              This recipe may have been removed or the link may be invalid.
            </p>
          </CardContent>
        </Card>
      </div>
    )
  }

  return (
    <div className="mx-auto max-w-3xl p-6 space-y-6">
      <div>
        <p className="text-xs text-muted-foreground uppercase tracking-wide mb-1">Shared Recipe</p>
        <h1 className="text-2xl font-bold">{recipe.name}</h1>
        {recipe.description && (
          <p className="mt-1 text-muted-foreground">{recipe.description}</p>
        )}
        <p className="mt-2 text-xs text-muted-foreground">
          Created {formatDate(recipe.created_at)}
        </p>
      </div>

      {recipe.variables && recipe.variables.length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle>Variables</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="space-y-3">
              {recipe.variables.map((variable) => (
                <div
                  key={variable.name}
                  className="flex items-center justify-between gap-4 rounded-lg border p-3"
                >
                  <div>
                    <div className="flex items-center gap-2">
                      <span className="font-medium">{variable.name}</span>
                      {variable.required && (
                        <span className="text-xs text-destructive">required</span>
                      )}
                    </div>
                    {variable.default && (
                      <p className="text-xs text-muted-foreground">
                        Default: {variable.default}
                      </p>
                    )}
                  </div>
                  <Badge
                    variant="secondary"
                    className={variableTypeBadgeStyles[variable.type] || ""}
                  >
                    {variable.type}
                  </Badge>
                </div>
              ))}
            </div>
          </CardContent>
        </Card>
      )}

      <Card>
        <CardHeader>
          <CardTitle>Steps</CardTitle>
        </CardHeader>
        <CardContent>
          {recipe.steps.length > 0 ? (
            <div className="space-y-4">
              {recipe.steps
                .sort((a, b) => a.order - b.order)
                .map((step, index) => (
                  <div key={step.id} className="flex gap-3">
                    <div className="flex flex-col items-center">
                      <div className="flex h-8 w-8 items-center justify-center rounded-full bg-primary text-primary-foreground text-sm font-medium">
                        {index + 1}
                      </div>
                      {index < recipe.steps.length - 1 && (
                        <div className="w-px flex-1 bg-border mt-2" />
                      )}
                    </div>
                    <div className="flex-1 pb-4">
                      <span className="text-sm font-medium">Step {index + 1}</span>
                      <pre className="mt-2 whitespace-pre-wrap rounded-md border bg-muted/50 p-3 text-sm font-mono">
                        {step.prompt_template}
                      </pre>
                    </div>
                  </div>
                ))}
            </div>
          ) : (
            <p className="text-sm text-muted-foreground">No steps defined</p>
          )}
        </CardContent>
      </Card>
    </div>
  )
}
