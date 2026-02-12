import { useState, useEffect } from "react"
import { Loader2, Play } from "lucide-react"
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
  onRunComplete: (recipeId: string, runId: string) => void
}

function getDefaultValue(variable: RecipeVariable): string {
  if (variable.default != null) return String(variable.default)
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

export function RecipeRunner({ open, onOpenChange, recipe, onRun, onRunComplete }: RecipeRunnerProps) {
  const [variables, setVariables] = useState<Record<string, string>>({})
  const [running, setRunning] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // Initialize variables when recipe changes
  useEffect(() => {
    if (recipe) {
      const initialValues: Record<string, string> = {}
      for (const v of recipe.variables || []) {
        initialValues[v.name] = getDefaultValue(v)
      }
      setVariables(initialValues)
      setError(null)
    }
  }, [recipe, open])

  const handleVariableChange = (name: string, value: string) => {
    setVariables((prev) => ({ ...prev, [name]: value }))
  }

  const handleRun = async () => {
    if (!recipe) return

    setRunning(true)
    setError(null)

    try {
      const run = await onRun(variables)
      onOpenChange(false)
      onRunComplete(recipe.id, run.id)
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

        {hasVariables && (
          <div className="space-y-4 max-h-[400px] overflow-y-auto pr-2">
            {recipe.variables.map((variable) => (
              <div key={variable.name} className="space-y-2">
                <div className="flex items-center gap-2">
                  <Label htmlFor={variable.name}>{variable.name}</Label>
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
            Cancel
          </Button>
          <Button onClick={handleRun} disabled={running}>
            {running ? (
              <Loader2 className="mr-1 h-4 w-4 animate-spin" />
            ) : (
              <Play className="mr-1 h-4 w-4" />
            )}
            Run Recipe
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
