import { useState, useEffect, useCallback } from "react"
import { ArrowLeft, Save, Play, Loader2, GripVertical, Clock, CheckCircle, XCircle, AlertCircle, Copy, Check, Link, Users, Globe } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Textarea } from "@/components/ui/textarea"
import { Badge } from "@/components/ui/badge"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion"
import type { Recipe, RecipeStep, RecipeRun } from "@/store/recipeSlice"

interface RecipeDetailProps {
  recipe: Recipe
  runs: RecipeRun[]
  onBack: () => void
  onSave: (data: Partial<Recipe>) => Promise<void>
  onRun: () => void
  onUpdateRun: (
    runId: string,
    data: { is_shared?: boolean; is_public?: boolean },
  ) => Promise<void>
}

const variableTypeBadgeStyles: Record<string, string> = {
  string: "bg-blue-100 text-blue-800 dark:bg-blue-900/30 dark:text-blue-400",
  number: "bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-400",
  date: "bg-purple-100 text-purple-800 dark:bg-purple-900/30 dark:text-purple-400",
  boolean: "bg-yellow-100 text-yellow-800 dark:bg-yellow-900/30 dark:text-yellow-400",
  select: "bg-orange-100 text-orange-800 dark:bg-orange-900/30 dark:text-orange-400",
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
      return <CheckCircle className="h-4 w-4 text-green-600" />
    case "failed":
      return <XCircle className="h-4 w-4 text-destructive" />
    case "running":
      return <Loader2 className="h-4 w-4 animate-spin text-blue-600" />
    case "pending":
      return <Clock className="h-4 w-4 text-muted-foreground" />
    default:
      return <AlertCircle className="h-4 w-4 text-muted-foreground" />
  }
}

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false)

  const handleCopy = useCallback(async () => {
    await navigator.clipboard.writeText(text)
    setCopied(true)
    setTimeout(() => setCopied(false), 2000)
  }, [text])

  return (
    <Button
      variant="outline"
      size="sm"
      onClick={handleCopy}
      data-testid="copy-share-link"
    >
      {copied ? (
        <Check className="mr-1 h-3 w-3" />
      ) : (
        <Copy className="mr-1 h-3 w-3" />
      )}
      {copied ? "Copied" : "Copy"}
    </Button>
  )
}

function getPublicUrl(path: string, token: string): string {
  return `${window.location.origin}${path}${token}/`
}

export function RecipeDetail({ recipe, runs, onBack, onSave, onRun, onUpdateRun }: RecipeDetailProps) {
  const [name, setName] = useState(recipe.name)
  const [description, setDescription] = useState(recipe.description)
  const [steps, setSteps] = useState<RecipeStep[]>(recipe.steps || [])
  const [saving, setSaving] = useState(false)
  const [hasChanges, setHasChanges] = useState(false)

  useEffect(() => {
    setName(recipe.name)
    setDescription(recipe.description)
    setSteps(recipe.steps || [])
    setHasChanges(false)
  }, [recipe])

  const handleSharingChange = useCallback(
    async (field: "is_shared" | "is_public", value: boolean) => {
      await onSave({ [field]: value })
    },
    [onSave],
  )

  const handleNameChange = (value: string) => {
    setName(value)
    setHasChanges(true)
  }

  const handleDescriptionChange = (value: string) => {
    setDescription(value)
    setHasChanges(true)
  }

  const handleStepChange = (stepId: string, promptTemplate: string) => {
    setSteps((prev) =>
      prev.map((s) => (s.id === stepId ? { ...s, prompt_template: promptTemplate } : s))
    )
    setHasChanges(true)
  }

  const handleSave = async () => {
    setSaving(true)
    try {
      await onSave({
        name,
        description,
        steps,
      })
      setHasChanges(false)
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between gap-4">
        <div className="flex items-center gap-4">
          <Button variant="ghost" size="sm" onClick={onBack}>
            <ArrowLeft className="mr-1 h-4 w-4" />
            Back
          </Button>
          <div>
            <h1 className="text-2xl font-bold">{recipe.name}</h1>
            <p className="text-sm text-muted-foreground">
              Created {formatDateTime(recipe.created_at)}
            </p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <Button variant="outline" onClick={onRun}>
            <Play className="mr-1 h-4 w-4" />
            Run
          </Button>
          <Button onClick={handleSave} disabled={saving || !hasChanges}>
            {saving ? (
              <Loader2 className="mr-1 h-4 w-4 animate-spin" />
            ) : (
              <Save className="mr-1 h-4 w-4" />
            )}
            Save
          </Button>
        </div>
      </div>

      {/* Basic Info */}
      <Card>
        <CardHeader>
          <CardTitle>Basic Information</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="space-y-2">
            <Label htmlFor="name">Name</Label>
            <Input
              id="name"
              value={name}
              onChange={(e) => handleNameChange(e.target.value)}
              placeholder="Recipe name"
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="description">Description</Label>
            <Textarea
              id="description"
              value={description}
              onChange={(e) => handleDescriptionChange(e.target.value)}
              placeholder="What does this recipe do?"
              rows={2}
            />
          </div>
        </CardContent>
      </Card>

      {/* Sharing */}
      <Card>
        <CardHeader>
          <CardTitle>Sharing</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <label
            className="flex items-start gap-3 cursor-pointer"
            data-testid="recipe-sharing-project"
          >
            <input
              type="checkbox"
              checked={recipe.is_shared}
              onChange={(e) => handleSharingChange("is_shared", e.target.checked)}
              className="mt-0.5 h-4 w-4 rounded border-gray-300"
            />
            <div className="flex-1">
              <div className="flex items-center gap-2">
                <Users className="h-4 w-4 text-muted-foreground" />
                <span className="text-sm font-medium">Share with project</span>
              </div>
              <p className="text-xs text-muted-foreground mt-0.5">
                All project members can view and run this recipe
              </p>
            </div>
          </label>

          <label
            className="flex items-start gap-3 cursor-pointer"
            data-testid="recipe-sharing-public"
          >
            <input
              type="checkbox"
              checked={recipe.is_public}
              onChange={(e) => handleSharingChange("is_public", e.target.checked)}
              className="mt-0.5 h-4 w-4 rounded border-gray-300"
            />
            <div className="flex-1">
              <div className="flex items-center gap-2">
                <Globe className="h-4 w-4 text-muted-foreground" />
                <span className="text-sm font-medium">Public link</span>
              </div>
              <p className="text-xs text-muted-foreground mt-0.5">
                Anyone with the link can view this recipe without signing in
              </p>
            </div>
          </label>

          {recipe.is_public && recipe.share_token && (
            <div
              className="flex items-center gap-2 rounded-md border bg-muted/50 p-2"
              data-testid="recipe-share-url"
            >
              <Link className="h-4 w-4 shrink-0 text-muted-foreground" />
              <code className="flex-1 truncate text-xs">
                {getPublicUrl("/api/recipes/shared/", recipe.share_token)}
              </code>
              <CopyButton
                text={getPublicUrl("/api/recipes/shared/", recipe.share_token)}
              />
            </div>
          )}
        </CardContent>
      </Card>

      {/* Variables */}
      <Card>
        <CardHeader>
          <CardTitle>Variables</CardTitle>
        </CardHeader>
        <CardContent>
          {recipe.variables && recipe.variables.length > 0 ? (
            <div className="space-y-3">
              {recipe.variables.map((variable) => (
                <div
                  key={variable.name}
                  className="flex items-center justify-between gap-4 rounded-lg border p-3"
                >
                  <div className="flex items-center gap-3">
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
                  </div>
                  <div className="flex items-center gap-2">
                    <Badge
                      variant="secondary"
                      className={variableTypeBadgeStyles[variable.type] || ""}
                    >
                      {variable.type}
                    </Badge>
                    {variable.type === "select" && variable.options && (
                      <span className="text-xs text-muted-foreground">
                        {variable.options.length} options
                      </span>
                    )}
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <p className="text-sm text-muted-foreground">No variables defined</p>
          )}
        </CardContent>
      </Card>

      {/* Steps */}
      <Card>
        <CardHeader>
          <CardTitle>Steps</CardTitle>
        </CardHeader>
        <CardContent>
          {steps.length > 0 ? (
            <div className="space-y-4">
              {steps
                .sort((a, b) => a.order - b.order)
                .map((step, index) => (
                  <div key={step.id} className="flex gap-3">
                    <div className="flex flex-col items-center">
                      <div className="flex h-8 w-8 items-center justify-center rounded-full bg-primary text-primary-foreground text-sm font-medium">
                        {index + 1}
                      </div>
                      {index < steps.length - 1 && (
                        <div className="w-px flex-1 bg-border mt-2" />
                      )}
                    </div>
                    <div className="flex-1 pb-4">
                      <div className="flex items-center gap-2 mb-2">
                        <GripVertical className="h-4 w-4 text-muted-foreground cursor-grab" />
                        <span className="text-sm font-medium">Step {index + 1}</span>
                      </div>
                      <Textarea
                        value={step.prompt_template}
                        onChange={(e) => handleStepChange(step.id, e.target.value)}
                        placeholder="Enter the prompt template for this step..."
                        rows={3}
                        className="font-mono text-sm"
                      />
                    </div>
                  </div>
                ))}
            </div>
          ) : (
            <p className="text-sm text-muted-foreground">No steps defined</p>
          )}
        </CardContent>
      </Card>

      {/* Run History */}
      <Card>
        <CardHeader className="pb-0">
          <Accordion type="single" collapsible className="w-full">
            <AccordionItem value="runs" className="border-none">
              <AccordionTrigger className="py-0 hover:no-underline">
                <CardTitle className="text-base">Run History</CardTitle>
              </AccordionTrigger>
              <AccordionContent className="pt-4">
                {runs.length > 0 ? (
                  <div className="space-y-2">
                    {runs.map((run) => (
                      <div
                        key={run.id}
                        className="rounded-lg border p-3 space-y-2"
                        data-testid={`recipe-run-${run.id}`}
                      >
                        <div className="flex items-center justify-between">
                          <div className="flex items-center gap-3">
                            {getStatusIcon(run.status)}
                            <div>
                              <div className="flex items-center gap-2">
                                <span className="text-sm font-medium capitalize">
                                  {run.status}
                                </span>
                              </div>
                              <p className="text-xs text-muted-foreground">
                                Started: {formatDateTime(run.started_at)}
                              </p>
                            </div>
                          </div>
                          <div className="text-right">
                            {run.variable_values && Object.keys(run.variable_values).length > 0 && (
                              <div className="flex flex-wrap gap-1 justify-end">
                                {Object.entries(run.variable_values)
                                  .slice(0, 3)
                                  .map(([key, value]) => (
                                    <Badge key={key} variant="outline" className="text-xs">
                                      {key}: {String(value).slice(0, 20)}
                                    </Badge>
                                  ))}
                              </div>
                            )}
                            {run.completed_at && (
                              <p className="text-xs text-muted-foreground mt-1">
                                Completed: {formatDateTime(run.completed_at)}
                              </p>
                            )}
                          </div>
                        </div>

                        {/* Run sharing controls */}
                        <div className="flex items-center gap-4 border-t pt-2">
                          <label className="flex items-center gap-1.5 cursor-pointer text-xs">
                            <input
                              type="checkbox"
                              checked={run.is_shared}
                              onChange={(e) =>
                                onUpdateRun(run.id, { is_shared: e.target.checked })
                              }
                              className="h-3.5 w-3.5 rounded border-gray-300"
                            />
                            <Users className="h-3 w-3 text-muted-foreground" />
                            <span className="text-muted-foreground">Project</span>
                          </label>
                          <label className="flex items-center gap-1.5 cursor-pointer text-xs">
                            <input
                              type="checkbox"
                              checked={run.is_public}
                              onChange={(e) =>
                                onUpdateRun(run.id, { is_public: e.target.checked })
                              }
                              className="h-3.5 w-3.5 rounded border-gray-300"
                            />
                            <Globe className="h-3 w-3 text-muted-foreground" />
                            <span className="text-muted-foreground">Public</span>
                          </label>
                          {run.is_public && run.share_token && (
                            <CopyButton
                              text={getPublicUrl(
                                "/api/recipes/runs/shared/",
                                run.share_token,
                              )}
                            />
                          )}
                        </div>
                      </div>
                    ))}
                  </div>
                ) : (
                  <p className="text-sm text-muted-foreground">No runs yet</p>
                )}
              </AccordionContent>
            </AccordionItem>
          </Accordion>
        </CardHeader>
      </Card>
    </div>
  )
}
