import { useState, useEffect } from "react"
import { Loader2, Play, CheckCircle, XCircle, Clock } from "lucide-react"
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
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Badge } from "@/components/ui/badge"
import type { Recipe, RecipeVariable, RecipeRun } from "@/store/recipeSlice"

interface RecipeRunnerProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  recipe: Recipe | null
  onRun: (variables: Record<string, string>) => Promise<RecipeRun>
}

function getDefaultValue(variable: RecipeVariable): string {
  if (variable.default) return variable.default
  switch (variable.type) {
    case "boolean":
      return "false"
    case "number":
      return "0"
    case "date":
      return new Date().toISOString().split("T")[0]
    case "select":
      return variable.options?.[0] ?? ""
    default:
      return ""
  }
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

export function RecipeRunner({ open, onOpenChange, recipe, onRun }: RecipeRunnerProps) {
  const [variables, setVariables] = useState<Record<string, string>>({})
  const [running, setRunning] = useState(false)
  const [result, setResult] = useState<RecipeRun | null>(null)
  const [error, setError] = useState<string | null>(null)

  // Initialize variables when recipe changes
  useEffect(() => {
    if (recipe) {
      const initialValues: Record<string, string> = {}
      for (const v of recipe.variables || []) {
        initialValues[v.name] = getDefaultValue(v)
      }
      setVariables(initialValues)
      setResult(null)
      setError(null)
    }
  }, [recipe, open])

  const handleVariableChange = (name: string, value: string) => {
    setVariables((prev) => ({ ...prev, [name]: value }))
  }

  const handleRun = async () => {
    if (!recipe) return

    // Validate required fields
    const missingRequired = (recipe.variables || [])
      .filter((v) => v.required && !variables[v.name])
      .map((v) => v.name)

    if (missingRequired.length > 0) {
      setError(`Missing required fields: ${missingRequired.join(", ")}`)
      return
    }

    setRunning(true)
    setError(null)
    setResult(null)

    try {
      const run = await onRun(variables)
      setResult(run)
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to run recipe")
    } finally {
      setRunning(false)
    }
  }

  const handleClose = () => {
    if (!running) {
      onOpenChange(false)
    }
  }

  if (!recipe) return null

  const hasVariables = recipe.variables && recipe.variables.length > 0

  return (
    <Dialog open={open} onOpenChange={handleClose}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>Run Recipe: {recipe.name}</DialogTitle>
          <DialogDescription>
            {hasVariables
              ? "Configure the variables below and run the recipe."
              : "This recipe has no variables. Click Run to execute."}
          </DialogDescription>
        </DialogHeader>

        {error && (
          <div className="rounded-md bg-destructive/10 p-3 text-sm text-destructive">
            {error}
          </div>
        )}

        {result && (
          <div className="rounded-md border p-4">
            <div className="flex items-center gap-3">
              {getStatusIcon(result.status)}
              <div>
                <p className="font-medium capitalize">{result.status}</p>
                <p className="text-sm text-muted-foreground">
                  {result.status === "running"
                    ? "Recipe is currently running..."
                    : result.status === "pending"
                      ? "Recipe is queued for execution..."
                      : result.status === "completed"
                        ? "Recipe completed successfully!"
                        : "Recipe execution failed."}
                </p>
              </div>
            </div>
            {result.step_results && result.step_results.length > 0 && (
              <div className="mt-3 pt-3 border-t">
                <p className="text-sm font-medium mb-2">Results:</p>
                <div className="space-y-2">
                  {result.step_results.map((stepResult, index) => (
                    <div key={index} className="text-sm bg-muted/50 p-2 rounded">
                      <span className="font-medium">Step {index + 1}:</span>{" "}
                      {typeof stepResult === "string"
                        ? stepResult
                        : JSON.stringify(stepResult, null, 2)}
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>
        )}

        {!result && hasVariables && (
          <div className="space-y-4 max-h-[400px] overflow-y-auto pr-2">
            {recipe.variables.map((variable) => (
              <div key={variable.name} className="space-y-2">
                <div className="flex items-center gap-2">
                  <Label htmlFor={variable.name}>{variable.name}</Label>
                  {variable.required && (
                    <span className="text-xs text-destructive">*</span>
                  )}
                  <Badge variant="outline" className="text-xs">
                    {variable.type}
                  </Badge>
                </div>

                {variable.type === "select" && variable.options ? (
                  <Select
                    value={variables[variable.name] || ""}
                    onValueChange={(value) => handleVariableChange(variable.name, value)}
                  >
                    <SelectTrigger className="w-full">
                      <SelectValue placeholder={`Select ${variable.name}`} />
                    </SelectTrigger>
                    <SelectContent>
                      {variable.options.map((option) => (
                        <SelectItem key={option} value={option}>
                          {option}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                ) : variable.type === "boolean" ? (
                  <Select
                    value={variables[variable.name] || "false"}
                    onValueChange={(value) => handleVariableChange(variable.name, value)}
                  >
                    <SelectTrigger className="w-full">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="true">True</SelectItem>
                      <SelectItem value="false">False</SelectItem>
                    </SelectContent>
                  </Select>
                ) : variable.type === "date" ? (
                  <Input
                    id={variable.name}
                    type="date"
                    value={variables[variable.name] || ""}
                    onChange={(e) => handleVariableChange(variable.name, e.target.value)}
                  />
                ) : variable.type === "number" ? (
                  <Input
                    id={variable.name}
                    type="number"
                    value={variables[variable.name] || ""}
                    onChange={(e) => handleVariableChange(variable.name, e.target.value)}
                    placeholder={`Enter ${variable.name}`}
                  />
                ) : (
                  <Input
                    id={variable.name}
                    type="text"
                    value={variables[variable.name] || ""}
                    onChange={(e) => handleVariableChange(variable.name, e.target.value)}
                    placeholder={`Enter ${variable.name}`}
                  />
                )}

                {variable.default && (
                  <p className="text-xs text-muted-foreground">
                    Default: {variable.default}
                  </p>
                )}
              </div>
            ))}
          </div>
        )}

        <DialogFooter>
          <Button variant="outline" onClick={handleClose} disabled={running}>
            {result ? "Close" : "Cancel"}
          </Button>
          {!result && (
            <Button onClick={handleRun} disabled={running}>
              {running ? (
                <Loader2 className="mr-1 h-4 w-4 animate-spin" />
              ) : (
                <Play className="mr-1 h-4 w-4" />
              )}
              Run Recipe
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
