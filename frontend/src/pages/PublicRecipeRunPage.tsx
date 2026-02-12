import { useState, useEffect } from "react"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import { Skeleton } from "@/components/ui/skeleton"

interface StepResult {
  step_order: number
  prompt: string
  response: string
  tools_used: string[]
  artifacts_created: string[]
  success: boolean
  error: string | null
  started_at: string
  completed_at: string | null
}

interface PublicRecipeRun {
  id: string
  status: "pending" | "running" | "completed" | "failed"
  variable_values: Record<string, string>
  step_results: StepResult[]
  started_at: string | null
  completed_at: string | null
  created_at: string
}

function formatDateTime(dateString: string | null | undefined): string {
  if (!dateString) return "-"
  return new Date(dateString).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  })
}

const statusStyles: Record<string, string> = {
  completed: "bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-400",
  failed: "bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-400",
  running: "bg-blue-100 text-blue-800 dark:bg-blue-900/30 dark:text-blue-400",
  pending: "bg-gray-100 text-gray-800 dark:bg-gray-900/30 dark:text-gray-400",
}

function getTokenFromPath(): string | undefined {
  // /shared/runs/{token}/ or /shared/runs/{token}
  const match = window.location.pathname.match(/^\/shared\/runs\/([^/]+)/)
  return match?.[1]
}

export function PublicRecipeRunPage() {
  const token = getTokenFromPath()
  const [run, setRun] = useState<PublicRecipeRun | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    if (!token) return
    fetch(`/api/recipes/runs/shared/${token}/`)
      .then((res) => {
        if (!res.ok) throw new Error(res.status === 404 ? "Run not found" : "Failed to load run")
        return res.json()
      })
      .then(setRun)
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false))
  }, [token])

  if (loading) {
    return (
      <div className="mx-auto max-w-3xl p-6 space-y-6">
        <Skeleton className="h-10 w-64" />
        <Skeleton className="h-4 w-48" />
        <Skeleton className="h-60 w-full" />
      </div>
    )
  }

  if (error || !run) {
    return (
      <div className="flex min-h-screen items-center justify-center">
        <Card className="w-full max-w-md">
          <CardContent className="pt-6 text-center">
            <p className="text-lg font-medium text-destructive">{error ?? "Run not found"}</p>
            <p className="mt-2 text-sm text-muted-foreground">
              This recipe run may have been removed or the link may be invalid.
            </p>
          </CardContent>
        </Card>
      </div>
    )
  }

  const variables = Object.entries(run.variable_values || {})

  return (
    <div className="mx-auto max-w-3xl p-6 space-y-6">
      <div>
        <p className="text-xs text-muted-foreground uppercase tracking-wide mb-1">Shared Recipe Run</p>
        <div className="flex items-center gap-3">
          <h1 className="text-2xl font-bold">Recipe Run</h1>
          <Badge className={statusStyles[run.status] || ""}>
            {run.status}
          </Badge>
        </div>
        <div className="mt-2 flex gap-4 text-xs text-muted-foreground">
          <span>Started: {formatDateTime(run.started_at)}</span>
          {run.completed_at && <span>Completed: {formatDateTime(run.completed_at)}</span>}
        </div>
      </div>

      {variables.length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle>Variables</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="grid gap-2">
              {variables.map(([key, value]) => (
                <div key={key} className="flex items-baseline gap-2">
                  <span className="text-sm font-medium">{key}:</span>
                  <span className="text-sm text-muted-foreground">{String(value)}</span>
                </div>
              ))}
            </div>
          </CardContent>
        </Card>
      )}

      <Card>
        <CardHeader>
          <CardTitle>Step Results</CardTitle>
        </CardHeader>
        <CardContent>
          {run.step_results && run.step_results.length > 0 ? (
            <div className="space-y-6">
              {run.step_results
                .sort((a, b) => a.step_order - b.step_order)
                .map((step, index) => (
                  <div key={index} className="flex gap-3">
                    <div className="flex flex-col items-center">
                      <div
                        className={`flex h-8 w-8 items-center justify-center rounded-full text-sm font-medium ${
                          step.success
                            ? "bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-400"
                            : step.error
                              ? "bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-400"
                              : "bg-primary text-primary-foreground"
                        }`}
                      >
                        {index + 1}
                      </div>
                      {index < run.step_results.length - 1 && (
                        <div className="w-px flex-1 bg-border mt-2" />
                      )}
                    </div>
                    <div className="flex-1 space-y-2 pb-4">
                      <div className="flex items-center gap-2">
                        <span className="text-sm font-medium">Step {index + 1}</span>
                        {step.tools_used.length > 0 && (
                          <div className="flex gap-1">
                            {step.tools_used.map((tool) => (
                              <Badge key={tool} variant="outline" className="text-xs">
                                {tool}
                              </Badge>
                            ))}
                          </div>
                        )}
                      </div>

                      <div className="rounded-md border bg-muted/50 p-3">
                        <p className="text-xs font-medium text-muted-foreground mb-1">Prompt</p>
                        <pre className="whitespace-pre-wrap text-sm font-mono">{step.prompt}</pre>
                      </div>

                      {step.response && (
                        <div className="rounded-md border p-3">
                          <p className="text-xs font-medium text-muted-foreground mb-1">Response</p>
                          <div className="prose prose-sm dark:prose-invert max-w-none whitespace-pre-wrap text-sm">
                            {step.response}
                          </div>
                        </div>
                      )}

                      {step.error && (
                        <div className="rounded-md border border-destructive/50 bg-destructive/10 p-3">
                          <p className="text-xs font-medium text-destructive mb-1">Error</p>
                          <p className="text-sm">{step.error}</p>
                        </div>
                      )}
                    </div>
                  </div>
                ))}
            </div>
          ) : (
            <p className="text-sm text-muted-foreground">No step results yet</p>
          )}
        </CardContent>
      </Card>
    </div>
  )
}
