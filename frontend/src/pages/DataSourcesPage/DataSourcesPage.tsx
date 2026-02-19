import { useEffect, useRef, useState, useCallback } from "react"
import { Link } from "react-router-dom"
import { CheckCircle, AlertCircle, Database, RefreshCw, Upload, Plus, Pencil, Trash2, Plug } from "lucide-react"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Badge } from "@/components/ui/badge"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { api } from "@/api/client"
import { useAppStore } from "@/store/store"
import type {
  DatabaseConnection,
  DatabaseConnectionFormData,
  ConnectionTestResult,
} from "./types"

interface CsvImportResult {
  table_name: string
  row_count: number
  column_count: number
  columns: { name: string; dtype: string }[]
}

function deriveTableName(filename: string): string {
  return filename
    .replace(/\.csv$/i, "")
    .toLowerCase()
    .replace(/[\s\-.@]+/g, "_")
    .replace(/[^a-z0-9_]/g, "")
    .replace(/^(\d)/, "col_$1")
}

const EMPTY_CONNECTION_FORM: DatabaseConnectionFormData = {
  name: "",
  description: "",
  db_host: "",
  db_port: 5432,
  db_name: "",
  db_user: "",
  db_password: "",
  is_active: true,
}

export function DataSourcesPage() {
  const [dbConnections, setDbConnections] = useState<DatabaseConnection[]>([])
  const [loading, setLoading] = useState(true)

  // CSV import state
  const [csvFile, setCsvFile] = useState<File | null>(null)
  const [tableName, setTableName] = useState("")
  const [uploading, setUploading] = useState(false)
  const [importResult, setImportResult] = useState<CsvImportResult | null>(null)
  const [importError, setImportError] = useState<string | null>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)

  // Connection dialog state
  const [connDialogOpen, setConnDialogOpen] = useState(false)
  const [editingConn, setEditingConn] = useState<DatabaseConnection | null>(null)
  const [connForm, setConnForm] = useState<DatabaseConnectionFormData>(EMPTY_CONNECTION_FORM)
  const [connFormError, setConnFormError] = useState<string | null>(null)
  const [savingConn, setSavingConn] = useState(false)
  const [testResult, setTestResult] = useState<ConnectionTestResult | null>(null)
  const [testing, setTesting] = useState(false)
  const [listTestConnId, setListTestConnId] = useState<string | null>(null)
  const [listTestResult, setListTestResult] = useState<ConnectionTestResult | null>(null)
  const [listTesting, setListTesting] = useState(false)

  const activeProjectId = useAppStore((s) => s.activeProjectId)
  const user = useAppStore((s) => s.user)
  const isAdmin = user?.is_staff ?? false

  const fetchData = useCallback(async () => {
    setLoading(true)
    if (isAdmin) {
      try {
        const conns = await api.get<DatabaseConnection[]>("/api/projects/connections/")
        setDbConnections(conns)
      } catch {
        setDbConnections([])
      }
    }
    setLoading(false)
  }, [isAdmin])

  useEffect(() => {
    fetchData()
  }, [fetchData])

  // --- CSV Import ---

  const handleFileSelect = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0] ?? null
    setCsvFile(file)
    setImportResult(null)
    setImportError(null)
    if (file) {
      setTableName(deriveTableName(file.name))
    } else {
      setTableName("")
    }
  }

  const handleCsvUpload = async () => {
    if (!csvFile || !tableName || !activeProjectId) return

    setUploading(true)
    setImportResult(null)
    setImportError(null)

    const formData = new FormData()
    formData.append("file", csvFile)
    formData.append("project_id", activeProjectId)
    formData.append("table_name", tableName)

    try {
      const result = await api.upload<CsvImportResult>(
        "/api/projects/csv-import/",
        formData,
      )
      setImportResult(result)
      setCsvFile(null)
      if (fileInputRef.current) {
        fileInputRef.current.value = ""
      }
    } catch (err) {
      const message = err instanceof Error ? err.message : "Upload failed"
      setImportError(message)
    } finally {
      setUploading(false)
    }
  }

  // --- Admin: Database Connection CRUD ---

  const openAddConnection = () => {
    setEditingConn(null)
    setConnForm(EMPTY_CONNECTION_FORM)
    setConnFormError(null)
    setTestResult(null)
    setConnDialogOpen(true)
  }

  const openEditConnection = (conn: DatabaseConnection) => {
    setEditingConn(conn)
    setConnForm({
      name: conn.name,
      description: conn.description,
      db_host: conn.db_host,
      db_port: conn.db_port,
      db_name: conn.db_name,
      db_user: "",
      db_password: "",
      is_active: conn.is_active,
    })
    setConnFormError(null)
    setTestResult(null)
    setConnDialogOpen(true)
  }

  const handleSaveConnection = async () => {
    setSavingConn(true)
    setConnFormError(null)
    try {
      const payload: Record<string, unknown> = { ...connForm }
      if (!payload.db_user) delete payload.db_user
      if (!payload.db_password) delete payload.db_password

      if (editingConn) {
        await api.put(`/api/projects/connections/${editingConn.id}/`, payload)
      } else {
        await api.post("/api/projects/connections/", payload)
      }
      setConnDialogOpen(false)
      fetchData()
    } catch (err) {
      setConnFormError(err instanceof Error ? err.message : "Save failed")
    } finally {
      setSavingConn(false)
    }
  }

  const handleDeleteConnection = async (conn: DatabaseConnection) => {
    if (!confirm(`Delete connection "${conn.name}"? This cannot be undone.`)) return
    try {
      await api.delete(`/api/projects/connections/${conn.id}/`)
      fetchData()
    } catch (err) {
      console.error("Failed to delete connection:", err)
    }
  }

  const handleTestConnection = async (connId: string, fromList = false) => {
    if (fromList) {
      setListTesting(true)
      setListTestConnId(connId)
      setListTestResult(null)
    } else {
      setTesting(true)
      setTestResult(null)
    }
    try {
      const result = await api.post<ConnectionTestResult>(
        `/api/projects/connections/${connId}/test_connection/`,
      )
      if (fromList) setListTestResult(result)
      else setTestResult(result)
    } catch (err) {
      const fail = { success: false, error: err instanceof Error ? err.message : "Test failed" } as const
      if (fromList) setListTestResult(fail)
      else setTestResult(fail)
    } finally {
      if (fromList) setListTesting(false)
      else setTesting(false)
    }
  }

  if (loading) {
    return (
      <div className="container mx-auto py-8">
        <div className="flex items-center justify-center">
          <RefreshCw className="h-6 w-6 animate-spin" />
          <span className="ml-2">Loading...</span>
        </div>
      </div>
    )
  }

  return (
    <div className="container mx-auto py-8">
      <div className="mb-6">
        <h1 className="text-2xl font-bold">Connections</h1>
        <p className="text-muted-foreground">
          Manage database connections and import data.
        </p>
      </div>

      {/* CSV Import */}
      {activeProjectId && (
        <Card className="mb-6" data-testid="csv-import-card">
          <CardHeader>
            <CardTitle className="flex items-center">
              <Upload className="mr-2 h-5 w-5" />
              Import CSV
            </CardTitle>
            <CardDescription>
              Upload a CSV file to import it as a table in your project's database.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <div className="space-y-4">
              <div className="grid gap-4 sm:grid-cols-2">
                <div className="space-y-2">
                  <Label htmlFor="csv-file">CSV File</Label>
                  <Input
                    id="csv-file"
                    ref={fileInputRef}
                    type="file"
                    accept=".csv"
                    onChange={handleFileSelect}
                    data-testid="csv-import-file"
                  />
                </div>
                <div className="space-y-2">
                  <Label htmlFor="table-name">Table Name</Label>
                  <Input
                    id="table-name"
                    value={tableName}
                    onChange={(e) => setTableName(e.target.value)}
                    placeholder="my_table"
                    data-testid="csv-import-table-name"
                  />
                </div>
              </div>
              <Button
                onClick={handleCsvUpload}
                disabled={!csvFile || !tableName || uploading}
                data-testid="csv-import-upload"
              >
                {uploading ? (
                  <RefreshCw className="mr-2 h-4 w-4 animate-spin" />
                ) : (
                  <Upload className="mr-2 h-4 w-4" />
                )}
                {uploading ? "Importing..." : "Upload & Import"}
              </Button>

              {importError && (
                <div className="rounded-md bg-red-50 p-3 text-sm text-red-800 dark:bg-red-900/20 dark:text-red-400">
                  <div className="flex items-center">
                    <AlertCircle className="mr-2 h-4 w-4 shrink-0" />
                    <span>{importError}</span>
                  </div>
                </div>
              )}

              {importResult && (
                <div className="rounded-md bg-green-50 p-3 text-sm text-green-800 dark:bg-green-900/20 dark:text-green-400">
                  <div className="flex items-center mb-2">
                    <CheckCircle className="mr-2 h-4 w-4 shrink-0" />
                    <span>
                      Imported {importResult.row_count.toLocaleString()} rows,{" "}
                      {importResult.column_count} columns into{" "}
                      <code className="rounded bg-green-100 px-1 dark:bg-green-900/40">
                        {importResult.table_name}
                      </code>
                      {" — "}
                      <Link
                        to="/data-dictionary"
                        className="underline hover:text-green-900 dark:hover:text-green-300"
                      >
                        View in Data Dictionary
                      </Link>
                    </span>
                  </div>
                  <div className="ml-6 text-xs text-green-700 dark:text-green-500">
                    {importResult.columns.map((c) => c.name).join(", ")}
                  </div>
                </div>
              )}
            </div>
          </CardContent>
        </Card>
      )}

      {/* Admin: Database Connections */}
      {isAdmin && (
        <Card data-testid="db-connections-card">
          <CardHeader>
            <div className="flex items-center justify-between">
              <div>
                <CardTitle className="flex items-center">
                  <Database className="mr-2 h-5 w-5" />
                  Database Connections
                </CardTitle>
                <CardDescription>
                  Manage database connections used by projects.
                </CardDescription>
              </div>
              <Button
                size="sm"
                onClick={openAddConnection}
                data-testid="db-connection-add"
              >
                <Plus className="mr-1 h-4 w-4" />
                Add Connection
              </Button>
            </div>
          </CardHeader>
          <CardContent>
            {dbConnections.length === 0 ? (
              <p className="text-sm text-muted-foreground">
                No database connections configured yet.
              </p>
            ) : (
              <div className="space-y-3">
                {dbConnections.map((conn) => (
                  <div key={conn.id} data-testid={`db-connection-item-${conn.id}`}>
                    <div className="flex items-center justify-between rounded-lg border p-4">
                      <div>
                        <h3 className="font-medium">{conn.name}</h3>
                        <p className="text-sm text-muted-foreground">
                          {conn.db_host}:{conn.db_port}/{conn.db_name}
                        </p>
                        {conn.description && (
                          <p className="text-xs text-muted-foreground">{conn.description}</p>
                        )}
                      </div>
                      <div className="flex items-center gap-2">
                        <Button
                          size="sm"
                          variant="ghost"
                          onClick={() => handleTestConnection(conn.id, true)}
                          disabled={listTesting && listTestConnId === conn.id}
                          data-testid={`db-connection-test-${conn.id}`}
                        >
                          {listTesting && listTestConnId === conn.id ? (
                            <RefreshCw className="h-4 w-4 animate-spin" />
                          ) : (
                            <Plug className="h-4 w-4" />
                          )}
                        </Button>
                        <Button
                          size="sm"
                          variant="ghost"
                          onClick={() => openEditConnection(conn)}
                          data-testid={`db-connection-edit-${conn.id}`}
                        >
                          <Pencil className="h-4 w-4" />
                        </Button>
                        <Button
                          size="sm"
                          variant="ghost"
                          onClick={() => handleDeleteConnection(conn)}
                          data-testid={`db-connection-delete-${conn.id}`}
                        >
                          <Trash2 className="h-4 w-4" />
                        </Button>
                        {conn.project_count > 0 && (
                          <Badge variant="secondary">
                            {conn.project_count} project{conn.project_count !== 1 ? "s" : ""}
                          </Badge>
                        )}
                      </div>
                    </div>
                    {listTestConnId === conn.id && listTestResult && (
                      <div className={`mt-1 rounded-md p-3 text-sm ${
                        listTestResult.success
                          ? "bg-green-50 text-green-800 dark:bg-green-900/20 dark:text-green-400"
                          : "bg-red-50 text-red-800 dark:bg-red-900/20 dark:text-red-400"
                      }`}>
                        {listTestResult.success ? (
                          <div>
                            <div className="flex items-center mb-1">
                              <CheckCircle className="mr-2 h-4 w-4" />
                              Connection successful
                            </div>
                            {listTestResult.schemas && listTestResult.schemas.length > 0 && (
                              <div className="text-xs ml-6">
                                Schemas: {listTestResult.schemas.join(", ")}
                              </div>
                            )}
                          </div>
                        ) : (
                          <div className="flex items-center">
                            <AlertCircle className="mr-2 h-4 w-4 shrink-0" />
                            {listTestResult.error}
                          </div>
                        )}
                      </div>
                    )}
                  </div>
                ))}
              </div>
            )}
          </CardContent>
        </Card>
      )}

      {/* Database Connection Dialog */}
      <Dialog open={connDialogOpen} onOpenChange={setConnDialogOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              {editingConn ? "Edit Database Connection" : "Add Database Connection"}
            </DialogTitle>
            <DialogDescription>
              {editingConn
                ? "Update the database connection settings."
                : "Configure a new database connection."}
            </DialogDescription>
          </DialogHeader>
          <div className="grid gap-4 py-4">
            <div className="grid gap-2">
              <Label htmlFor="conn-name">Name</Label>
              <Input
                id="conn-name"
                value={connForm.name}
                onChange={(e) => setConnForm((f) => ({ ...f, name: e.target.value }))}
                placeholder="Production Analytics DB"
                data-testid="db-connection-form-name"
              />
            </div>
            <div className="grid gap-2">
              <Label htmlFor="conn-description">Description</Label>
              <Input
                id="conn-description"
                value={connForm.description}
                onChange={(e) => setConnForm((f) => ({ ...f, description: e.target.value }))}
                placeholder="Optional description"
                data-testid="db-connection-form-description"
              />
            </div>
            <div className="grid grid-cols-3 gap-4">
              <div className="col-span-2 grid gap-2">
                <Label htmlFor="conn-host">Host</Label>
                <Input
                  id="conn-host"
                  value={connForm.db_host}
                  onChange={(e) => setConnForm((f) => ({ ...f, db_host: e.target.value }))}
                  placeholder="localhost"
                  data-testid="db-connection-form-host"
                />
              </div>
              <div className="grid gap-2">
                <Label htmlFor="conn-port">Port</Label>
                <Input
                  id="conn-port"
                  type="number"
                  value={connForm.db_port}
                  onChange={(e) => setConnForm((f) => ({ ...f, db_port: parseInt(e.target.value) || 5432 }))}
                  data-testid="db-connection-form-port"
                />
              </div>
            </div>
            <div className="grid gap-2">
              <Label htmlFor="conn-dbname">Database Name</Label>
              <Input
                id="conn-dbname"
                value={connForm.db_name}
                onChange={(e) => setConnForm((f) => ({ ...f, db_name: e.target.value }))}
                placeholder="analytics"
                data-testid="db-connection-form-dbname"
              />
            </div>
            <div className="grid grid-cols-2 gap-4">
              <div className="grid gap-2">
                <Label htmlFor="conn-user">
                  Username
                  {editingConn && (
                    <span className="text-muted-foreground font-normal"> (leave blank to keep)</span>
                  )}
                </Label>
                <Input
                  id="conn-user"
                  value={connForm.db_user ?? ""}
                  onChange={(e) => setConnForm((f) => ({ ...f, db_user: e.target.value }))}
                  data-testid="db-connection-form-user"
                />
              </div>
              <div className="grid gap-2">
                <Label htmlFor="conn-password">
                  Password
                  {editingConn && (
                    <span className="text-muted-foreground font-normal"> (leave blank to keep)</span>
                  )}
                </Label>
                <Input
                  id="conn-password"
                  type="password"
                  value={connForm.db_password ?? ""}
                  onChange={(e) => setConnForm((f) => ({ ...f, db_password: e.target.value }))}
                  data-testid="db-connection-form-password"
                />
              </div>
            </div>

            {/* Test result shown inside dialog when editing */}
            {editingConn && testResult && (
              <div className={`rounded-md p-3 text-sm ${
                testResult.success
                  ? "bg-green-50 text-green-800 dark:bg-green-900/20 dark:text-green-400"
                  : "bg-red-50 text-red-800 dark:bg-red-900/20 dark:text-red-400"
              }`}>
                {testResult.success ? (
                  <div>
                    <div className="flex items-center mb-1">
                      <CheckCircle className="mr-2 h-4 w-4" />
                      Connection successful
                    </div>
                    {testResult.schemas && testResult.schemas.length > 0 && (
                      <div className="text-xs ml-6">
                        Schemas: {testResult.schemas.join(", ")}
                      </div>
                    )}
                  </div>
                ) : (
                  <div className="flex items-center">
                    <AlertCircle className="mr-2 h-4 w-4 shrink-0" />
                    {testResult.error}
                  </div>
                )}
              </div>
            )}

            {connFormError && (
              <div className="text-sm text-red-600">{connFormError}</div>
            )}
          </div>
          <DialogFooter>
            {editingConn && (
              <Button
                variant="outline"
                onClick={() => handleTestConnection(editingConn.id)}
                disabled={testing}
                data-testid="db-connection-form-test"
              >
                {testing ? (
                  <RefreshCw className="mr-2 h-4 w-4 animate-spin" />
                ) : (
                  <Plug className="mr-2 h-4 w-4" />
                )}
                Test
              </Button>
            )}
            <Button variant="outline" onClick={() => setConnDialogOpen(false)}>
              Cancel
            </Button>
            <Button
              onClick={handleSaveConnection}
              disabled={savingConn || !connForm.name || !connForm.db_host || !connForm.db_name}
              data-testid="db-connection-form-save"
            >
              {savingConn && <RefreshCw className="mr-2 h-4 w-4 animate-spin" />}
              {editingConn ? "Update" : "Create"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}
