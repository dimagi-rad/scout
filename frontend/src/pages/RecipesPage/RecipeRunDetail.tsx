import Markdown from "react-markdown"
import remarkGfm from "remark-gfm"
import {
  ArrowLeft,
  CheckCircle,
  XCircle,
  Loader2,
  Clock,
  AlertCircle,
  Users,
  FileBarChart,
} from "lucide-react"
import { Button } from "@/components/ui/button"
import { Badge } from "@/components/ui/badge"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { useAppStore } from "@/store/store"
import type { Recipe, RecipeRun } from "@/store/recipeSlice"

interface RecipeRunDetailProps {
  recipe: Recipe
  run: RecipeRun
  onBack: () => void
  onUpdateRun: (
    runId: string,
    data: { is_shared?: boolean; is_public?: boolean },
  ) => Promise<void>
}

function formatDateTime(dateString: string | null | undefined): string {
  if (!dateString) return "-"
  const date = new Date(dateString)
  return date.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  })
}

function getStatusIcon(status: RecipeRun["status"]) {
  switch (status) {
    case "completed":
      return <CheckCircle className="h-5 w-5 text-green-600" />
    case "failed":
      return <XCircle className="h-5 w-5 text-destructive" />
    case "running":
      return <Loader2 className="h-5 w-5 animate-spin text-blue-600" />
    case "pending":
      return <Clock className="h-5 w-5 text-muted-foreground" />
  }
}

function getStatusBadgeClass(status: RecipeRun["status"]): string {
  switch (status) {
    case "completed":
      return "bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-400"
    case "failed":
      return "bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-400"
    case "running":
      return "bg-blue-100 text-blue-800 dark:bg-blue-900/30 dark:text-blue-400"
    case "pending":
      return "bg-gray-100 text-gray-800 dark:bg-gray-900/30 dark:text-gray-400"
  }
}

export function RecipeRunDetail({ recipe, run, onBack, onUpdateRun }: RecipeRunDetailProps) {
  const openArtifact = useAppStore((s) => s.uiActions.openArtifact)
  const activeArtifactId = useAppStore((s) => s.activeArtifactId)
  const variableEntries = run.variable_values
    ? Object.entries(run.variable_values)
    : []

  return (
    <div className="space-y-6">
      <div className="flex items-center gap-4">
        <Button variant="ghost" size="sm" onClick={onBack} data-testid="run-detail-back">
          <ArrowLeft className="mr-1 h-4 w-4" />
          Back
        </Button>
        <div className="flex-1">
          <p className="text-sm text-muted-foreground">{recipe.name}</p>
          <div className="flex items-center gap-3">
            <h1 className="text-2xl font-bold">Run Details</h1>
            <Badge className={getStatusBadgeClass(run.status)} data-testid="run-detail-status">
              {run.status}
            </Badge>
          </div>
        </div>
      </div>

      <Card>
        <CardContent className="pt-6">
          <div className="flex items-center gap-3 mb-4">
            {getStatusIcon(run.status)}
            <span className="text-lg font-medium capitalize">{run.status}</span>
          </div>
          <div className="grid grid-cols-2 gap-4 text-sm">
            <div>
              <span className="text-muted-foreground">Started</span>
              <p className="font-medium">{formatDateTime(run.started_at)}</p>
            </div>
            <div>
              <span className="text-muted-foreground">Completed</span>
              <p className="font-medium">{formatDateTime(run.completed_at)}</p>
            </div>
          </div>
        </CardContent>
      </Card>

      {variableEntries.length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle>Variables</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="space-y-2">
              {variableEntries.map(([key, value]) => (
                <div
                  key={key}
                  className="flex items-center justify-between gap-4 rounded-lg border p-3"
                >
                  <span className="text-sm font-medium">{key}</span>
                  <span className="text-sm text-muted-foreground truncate max-w-[60%] text-right">
                    {String(value)}
                  </span>
                </div>
              ))}
            </div>
          </CardContent>
        </Card>
      )}

      {run.step_results && run.step_results.length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle>Step Results</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            {run.step_results.map((step, index) => (
              <div key={index} className="rounded border bg-muted/30" data-testid={`run-step-${step.step_order}`}>
                <div className="flex items-center gap-2 px-3 py-2 border-b bg-muted/50">
                  {step.success ? (
                    <CheckCircle className="h-3.5 w-3.5 text-green-600 shrink-0" />
                  ) : (
                    <AlertCircle className="h-3.5 w-3.5 text-destructive shrink-0" />
                  )}
                  <span className="text-sm font-medium">Step {step.step_order}</span>
                  {step.tools_used.length > 0 && (
                    <div className="flex gap-1 ml-auto">
                      {step.tools_used.map((tool) => (
                        <Badge key={tool} variant="secondary" className="text-xs">
                          {tool}
                        </Badge>
                      ))}
                    </div>
                  )}
                </div>
                <div className="p-3 text-sm prose prose-sm dark:prose-invert max-w-none prose-p:my-1 prose-headings:my-2 prose-table:my-2 prose-pre:my-1 overflow-x-auto">
                  {step.error ? (
                    <p className="text-destructive">{step.error}</p>
                  ) : (
                    <Markdown remarkPlugins={[remarkGfm]}>{step.response}</Markdown>
                  )}
                </div>
                {step.artifacts_created && step.artifacts_created.length > 0 && (
                  <div className="flex flex-wrap gap-2 px-3 pb-3 border-t pt-2">
                    {step.artifacts_created.map((artifactId) => (
                      <button
                        key={artifactId}
                        onClick={() => openArtifact(artifactId)}
                        className={`flex items-center gap-2 rounded-lg border px-3 py-1.5 text-sm transition-colors hover:bg-muted ${
                          activeArtifactId === artifactId
                            ? "border-primary bg-primary/5"
                            : "border-border"
                        }`}
                        data-testid={`run-artifact-${artifactId}`}
                      >
                        <FileBarChart className="h-4 w-4 text-primary" />
                        <span>View Artifact</span>
                      </button>
                    ))}
                  </div>
                )}
              </div>
            ))}
          </CardContent>
        </Card>
      )}

      <Card>
        <CardHeader>
          <CardTitle>Sharing</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <label
            className="flex items-center gap-1.5 cursor-pointer text-sm"
            data-testid="run-sharing-project"
          >
            <input
              type="checkbox"
              checked={run.is_shared}
              onChange={(e) =>
                onUpdateRun(run.id, { is_shared: e.target.checked })
              }
              className="h-4 w-4 rounded border-gray-300"
            />
            <Users className="h-4 w-4 text-muted-foreground" />
            <span>Share with project</span>
          </label>
        </CardContent>
      </Card>
    </div>
  )
}
