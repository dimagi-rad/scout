import { useEffect, useState } from "react"
import { useNavigate, useParams } from "react-router-dom"
import { ArrowLeft, Loader2, CheckCircle, XCircle } from "lucide-react"
import { useAppStore } from "@/store/store"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Textarea } from "@/components/ui/textarea"
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion"
import { Card, CardContent, CardDescription, CardHeader } from "@/components/ui/card"
import { api } from "@/api/client"
import type { ProjectFormData } from "@/store/projectSlice"

interface ConnectionTestResult {
  success: boolean
  schemas?: string[]
  tables?: { schema: string; table: string }[]
  error?: string
}

interface FormState {
  name: string
  slug: string
  description: string
  db_host: string
  db_port: number
  db_name: string
  db_schema: string
  db_user: string
  db_password: string
  allowed_tables: string
  excluded_tables: string
  system_prompt: string
  llm_model: string
}

const initialFormState: FormState = {
  name: "",
  slug: "",
  description: "",
  db_host: "",
  db_port: 5432,
  db_name: "",
  db_schema: "public",
  db_user: "",
  db_password: "",
  allowed_tables: "",
  excluded_tables: "",
  system_prompt: "",
  llm_model: "claude-sonnet-4-5-20250929",
}

export function ProjectForm() {
  const { id } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const isEdit = !!id

  const fetchProject = useAppStore((s) => s.projectActions.fetchProject)
  const createProject = useAppStore((s) => s.projectActions.createProject)
  const updateProject = useAppStore((s) => s.projectActions.updateProject)

  const [form, setForm] = useState<FormState>(initialFormState)
  const [loading, setLoading] = useState(false)
  const [fetchingProject, setFetchingProject] = useState(false)
  const [testingConnection, setTestingConnection] = useState(false)
  const [connectionResult, setConnectionResult] = useState<ConnectionTestResult | null>(null)
  const [error, setError] = useState<string | null>(null)

  // Load project data when editing
  useEffect(() => {
    if (isEdit && id) {
      setFetchingProject(true)
      fetchProject(id)
        .then((project) => {
          setForm({
            name: project.name,
            slug: project.slug,
            description: project.description,
            db_host: project.db_host,
            db_port: project.db_port,
            db_name: project.db_name,
            db_schema: project.db_schema,
            db_user: project.db_user,
            db_password: "",  // Password is write-only, never returned from API
            allowed_tables: (project.allowed_tables || []).join(", "),
            excluded_tables: (project.excluded_tables || []).join(", "),
            system_prompt: project.system_prompt,
            llm_model: project.llm_model,
          })
        })
        .catch(() => {
          setError("Failed to load project")
        })
        .finally(() => {
          setFetchingProject(false)
        })
    }
  }, [id, isEdit, fetchProject])

  const handleChange = (
    e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement>
  ) => {
    const { name, value, type } = e.target
    setForm((prev) => ({
      ...prev,
      [name]: type === "number" ? Number(value) : value,
    }))
  }

  const handleTestConnection = async () => {
    setTestingConnection(true)
    setConnectionResult(null)

    try {
      const result = await api.post<ConnectionTestResult>(
        "/api/projects/test-connection/",
        {
          db_host: form.db_host,
          db_port: form.db_port,
          db_name: form.db_name,
          db_user: form.db_user,
          db_password: form.db_password,
        }
      )
      setConnectionResult(result)
    } catch (err) {
      setConnectionResult({
        success: false,
        error: err instanceof Error ? err.message : "Connection test failed",
      })
    } finally {
      setTestingConnection(false)
    }
  }

  const parseCommaSeparated = (value: string): string[] => {
    return value
      .split(",")
      .map((s) => s.trim())
      .filter((s) => s.length > 0)
  }

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setLoading(true)
    setError(null)

    const projectData: ProjectFormData = {
      name: form.name,
      slug: form.slug,
      description: form.description,
      db_host: form.db_host,
      db_port: form.db_port,
      db_name: form.db_name,
      db_schema: form.db_schema,
      db_user: form.db_user,
      db_password: form.db_password || undefined,
      allowed_tables: parseCommaSeparated(form.allowed_tables),
      excluded_tables: parseCommaSeparated(form.excluded_tables),
      system_prompt: form.system_prompt,
      llm_model: form.llm_model,
    }

    try {
      if (isEdit && id) {
        await updateProject(id, projectData)
      } else {
        await createProject(projectData)
      }
      navigate("/projects")
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to save project")
    } finally {
      setLoading(false)
    }
  }

  if (fetchingProject) {
    return (
      <div className="container mx-auto py-8">
        <div className="flex items-center justify-center">
          <Loader2 className="h-6 w-6 animate-spin" />
          <span className="ml-2">Loading project...</span>
        </div>
      </div>
    )
  }

  return (
    <div className="container mx-auto py-8">
      <div className="mb-6">
        <Button variant="ghost" onClick={() => navigate("/projects")}>
          <ArrowLeft className="mr-2 h-4 w-4" />
          Back to Projects
        </Button>
      </div>

      <Card className="mx-auto max-w-2xl">
        <CardHeader>
          <h1 className="text-2xl font-bold">
            {isEdit ? "Edit Project" : "Create Project"}
          </h1>
          <CardDescription>
            {isEdit
              ? "Update your project settings"
              : "Set up a new data project with database connection"}
          </CardDescription>
        </CardHeader>
        <CardContent>
          <form onSubmit={handleSubmit}>
            {error && (
              <div className="mb-4 rounded-md bg-destructive/10 p-3 text-sm text-destructive">
                {error}
              </div>
            )}

            <Accordion
              type="multiple"
              defaultValue={["basic", "database"]}
              className="w-full"
            >
              {/* Basic Info Section */}
              <AccordionItem value="basic">
                <AccordionTrigger>Basic Info</AccordionTrigger>
                <AccordionContent>
                  <div className="space-y-4">
                    <div className="space-y-2">
                      <Label htmlFor="name">Project Name</Label>
                      <Input
                        id="name"
                        name="name"
                        value={form.name}
                        onChange={handleChange}
                        placeholder="My Data Project"
                        required
                      />
                    </div>

                    <div className="space-y-2">
                      <Label htmlFor="slug">Slug</Label>
                      <Input
                        id="slug"
                        name="slug"
                        value={form.slug}
                        onChange={handleChange}
                        placeholder="my-data-project"
                        required
                      />
                      <p className="text-xs text-muted-foreground">
                        URL-friendly identifier (lowercase, hyphens only)
                      </p>
                    </div>

                    <div className="space-y-2">
                      <Label htmlFor="description">Description</Label>
                      <Textarea
                        id="description"
                        name="description"
                        value={form.description}
                        onChange={handleChange}
                        placeholder="Describe your project..."
                        rows={3}
                      />
                    </div>
                  </div>
                </AccordionContent>
              </AccordionItem>

              {/* Database Connection Section */}
              <AccordionItem value="database">
                <AccordionTrigger>Database Connection</AccordionTrigger>
                <AccordionContent>
                  <div className="space-y-4">
                    <div className="grid grid-cols-2 gap-4">
                      <div className="space-y-2">
                        <Label htmlFor="db_host">Host</Label>
                        <Input
                          id="db_host"
                          name="db_host"
                          value={form.db_host}
                          onChange={handleChange}
                          placeholder="localhost"
                          required
                        />
                      </div>

                      <div className="space-y-2">
                        <Label htmlFor="db_port">Port</Label>
                        <Input
                          id="db_port"
                          name="db_port"
                          type="number"
                          value={form.db_port}
                          onChange={handleChange}
                          required
                        />
                      </div>
                    </div>

                    <div className="space-y-2">
                      <Label htmlFor="db_name">Database Name</Label>
                      <Input
                        id="db_name"
                        name="db_name"
                        value={form.db_name}
                        onChange={handleChange}
                        placeholder="my_database"
                        required
                      />
                    </div>

                    <div className="grid grid-cols-2 gap-4">
                      <div className="space-y-2">
                        <Label htmlFor="db_user">User</Label>
                        <Input
                          id="db_user"
                          name="db_user"
                          value={form.db_user}
                          onChange={handleChange}
                          placeholder="postgres"
                          required
                        />
                      </div>

                      <div className="space-y-2">
                        <Label htmlFor="db_password">Password</Label>
                        <Input
                          id="db_password"
                          name="db_password"
                          type="password"
                          value={form.db_password}
                          onChange={handleChange}
                          placeholder={isEdit ? "(unchanged)" : ""}
                        />
                      </div>
                    </div>

                    <div className="pt-2">
                      <Button
                        type="button"
                        variant="outline"
                        onClick={handleTestConnection}
                        disabled={testingConnection}
                      >
                        {testingConnection && (
                          <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                        )}
                        Test Connection
                      </Button>

                      {connectionResult && (
                        <div
                          className={`mt-3 rounded-md p-3 ${
                            connectionResult.success
                              ? "bg-green-50 text-green-800 dark:bg-green-900/20 dark:text-green-400"
                              : "bg-red-50 text-red-800 dark:bg-red-900/20 dark:text-red-400"
                          }`}
                        >
                          <div className="flex items-center">
                            {connectionResult.success ? (
                              <CheckCircle className="mr-2 h-4 w-4" />
                            ) : (
                              <XCircle className="mr-2 h-4 w-4" />
                            )}
                            <span className="font-medium">
                              {connectionResult.success
                                ? "Connection successful!"
                                : "Connection failed"}
                            </span>
                          </div>
                          {connectionResult.success && connectionResult.schemas && (
                            <p className="mt-1 text-sm">
                              Found {connectionResult.schemas.length} schema(s)
                              {connectionResult.tables &&
                                `, ${connectionResult.tables.length} table(s)`}
                            </p>
                          )}
                          {connectionResult.error && (
                            <p className="mt-1 text-sm">{connectionResult.error}</p>
                          )}
                        </div>
                      )}
                    </div>
                  </div>
                </AccordionContent>
              </AccordionItem>

              {/* Access Control Section */}
              <AccordionItem value="access">
                <AccordionTrigger>Access Control</AccordionTrigger>
                <AccordionContent>
                  <div className="space-y-4">
                    <div className="space-y-2">
                      <Label htmlFor="db_schema">Schema</Label>
                      <Input
                        id="db_schema"
                        name="db_schema"
                        value={form.db_schema}
                        onChange={handleChange}
                        placeholder="public"
                      />
                      <p className="text-xs text-muted-foreground">
                        Database schema to use for this project
                      </p>
                    </div>

                    <div className="space-y-2">
                      <Label htmlFor="allowed_tables">Allowed Tables</Label>
                      <Input
                        id="allowed_tables"
                        name="allowed_tables"
                        value={form.allowed_tables}
                        onChange={handleChange}
                        placeholder="users, orders, products"
                      />
                      <p className="text-xs text-muted-foreground">
                        Comma-separated list of tables to allow access (leave
                        empty for all)
                      </p>
                    </div>

                    <div className="space-y-2">
                      <Label htmlFor="excluded_tables">Excluded Tables</Label>
                      <Input
                        id="excluded_tables"
                        name="excluded_tables"
                        value={form.excluded_tables}
                        onChange={handleChange}
                        placeholder="sensitive_data, audit_logs"
                      />
                      <p className="text-xs text-muted-foreground">
                        Comma-separated list of tables to exclude from access
                      </p>
                    </div>
                  </div>
                </AccordionContent>
              </AccordionItem>

              {/* AI Configuration Section */}
              <AccordionItem value="ai">
                <AccordionTrigger>AI Configuration</AccordionTrigger>
                <AccordionContent>
                  <div className="space-y-4">
                    <div className="space-y-2">
                      <Label htmlFor="system_prompt">System Prompt</Label>
                      <Textarea
                        id="system_prompt"
                        name="system_prompt"
                        value={form.system_prompt}
                        onChange={handleChange}
                        placeholder="You are a helpful data analyst assistant..."
                        rows={4}
                      />
                      <p className="text-xs text-muted-foreground">
                        Custom instructions for the AI when querying this project
                      </p>
                    </div>

                    <div className="space-y-2">
                      <Label htmlFor="llm_model">LLM Model</Label>
                      <Input
                        id="llm_model"
                        name="llm_model"
                        value={form.llm_model}
                        onChange={handleChange}
                        placeholder="claude-sonnet-4-5-20250929"
                      />
                    </div>
                  </div>
                </AccordionContent>
              </AccordionItem>
            </Accordion>

            <div className="mt-6 flex justify-end gap-3">
              <Button
                type="button"
                variant="outline"
                onClick={() => navigate("/projects")}
              >
                Cancel
              </Button>
              <Button type="submit" disabled={loading}>
                {loading && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
                {isEdit ? "Save Changes" : "Create Project"}
              </Button>
            </div>
          </form>
        </CardContent>
      </Card>
    </div>
  )
}
