import { useEffect, useState } from "react"
import { useSearchParams } from "react-router-dom"
import { CheckCircle, AlertCircle, Database, Cloud, RefreshCw } from "lucide-react"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Badge } from "@/components/ui/badge"
import { api } from "@/api/client"
import type {
  DataSourceCredential,
  MaterializedDataset,
  DataSource,
} from "./types"

export function DataSourcesPage() {
  const [searchParams] = useSearchParams()
  const [credentials, setCredentials] = useState<DataSourceCredential[]>([])
  const [datasets, setDatasets] = useState<MaterializedDataset[]>([])
  const [dataSources, setDataSources] = useState<DataSource[]>([])
  const [loading, setLoading] = useState(true)
  const [connectingSource, setConnectingSource] = useState<string | null>(null)

  const successMessage = searchParams.get("success") === "true"
  const errorMessage = searchParams.get("error")

  useEffect(() => {
    fetchData()
  }, [])

  const fetchData = async () => {
    setLoading(true)
    try {
      const [credentialsRes, datasetsRes, sourcesRes] = await Promise.all([
        api.get<DataSourceCredential[]>("/api/datasources/credentials/"),
        api.get<MaterializedDataset[]>("/api/datasources/datasets/"),
        api.get<DataSource[]>("/api/datasources/sources/"),
      ])
      setCredentials(credentialsRes)
      setDatasets(datasetsRes)
      setDataSources(sourcesRes)
    } catch (err) {
      console.error("Failed to fetch data sources:", err)
    } finally {
      setLoading(false)
    }
  }

  const handleConnect = async (dataSourceId: string) => {
    setConnectingSource(dataSourceId)
    try {
      const response = await api.post<{ authorization_url: string }>(
        "/api/datasources/oauth/start/",
        { data_source_id: dataSourceId }
      )
      window.location.href = response.authorization_url
    } catch (err) {
      console.error("Failed to start OAuth flow:", err)
      setConnectingSource(null)
    }
  }

  const handleTriggerSync = async (datasetId: string) => {
    try {
      await api.post(`/api/datasources/datasets/${datasetId}/trigger_sync/`)
      fetchData()
    } catch (err) {
      console.error("Failed to trigger sync:", err)
    }
  }

  const getStatusBadge = (status: string) => {
    const variants: Record<string, "default" | "secondary" | "destructive" | "outline"> = {
      ready: "default",
      syncing: "secondary",
      pending: "outline",
      error: "destructive",
      stale: "secondary",
    }
    return <Badge variant={variants[status] || "outline"}>{status}</Badge>
  }

  const isConnected = (dataSourceId: string) => {
    return credentials.some(
      (c) => c.data_source === dataSourceId && c.is_valid
    )
  }

  if (loading) {
    return (
      <div className="container mx-auto py-8">
        <div className="flex items-center justify-center">
          <RefreshCw className="h-6 w-6 animate-spin" />
          <span className="ml-2">Loading data sources...</span>
        </div>
      </div>
    )
  }

  return (
    <div className="container mx-auto py-8">
      <div className="mb-6">
        <h1 className="text-2xl font-bold">Data Sources</h1>
        <p className="text-muted-foreground">
          Connect external data sources to make their data available for querying.
        </p>
      </div>

      {successMessage && (
        <div className="mb-4 rounded-md bg-green-50 p-4 text-green-800 dark:bg-green-900/20 dark:text-green-400">
          <div className="flex items-center">
            <CheckCircle className="mr-2 h-4 w-4" />
            <span>Successfully connected to data source!</span>
          </div>
        </div>
      )}

      {errorMessage && (
        <div className="mb-4 rounded-md bg-red-50 p-4 text-red-800 dark:bg-red-900/20 dark:text-red-400">
          <div className="flex items-center">
            <AlertCircle className="mr-2 h-4 w-4" />
            <span>Error: {errorMessage}</span>
          </div>
        </div>
      )}

      <div className="grid gap-6 md:grid-cols-2">
        {/* Available Data Sources */}
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center">
              <Cloud className="mr-2 h-5 w-5" />
              Available Sources
            </CardTitle>
            <CardDescription>
              Connect to external data sources using OAuth.
            </CardDescription>
          </CardHeader>
          <CardContent>
            {dataSources.length === 0 ? (
              <p className="text-sm text-muted-foreground">
                No data sources configured. Contact an administrator.
              </p>
            ) : (
              <div className="space-y-4">
                {dataSources.map((source) => (
                  <div
                    key={source.id}
                    className="flex items-center justify-between rounded-lg border p-4"
                  >
                    <div>
                      <h3 className="font-medium">{source.name}</h3>
                      <p className="text-sm text-muted-foreground">
                        {source.source_type_display}
                      </p>
                    </div>
                    {isConnected(source.id) ? (
                      <Badge variant="default" className="bg-green-600">
                        <CheckCircle className="mr-1 h-3 w-3" />
                        Connected
                      </Badge>
                    ) : (
                      <Button
                        size="sm"
                        onClick={() => handleConnect(source.id)}
                        disabled={connectingSource === source.id}
                      >
                        {connectingSource === source.id ? (
                          <RefreshCw className="mr-2 h-4 w-4 animate-spin" />
                        ) : null}
                        Connect
                      </Button>
                    )}
                  </div>
                ))}
              </div>
            )}
          </CardContent>
        </Card>

        {/* My Credentials */}
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center">
              <Database className="mr-2 h-5 w-5" />
              My Connections
            </CardTitle>
            <CardDescription>
              Your connected data source credentials.
            </CardDescription>
          </CardHeader>
          <CardContent>
            {credentials.length === 0 ? (
              <p className="text-sm text-muted-foreground">
                No connections yet. Connect a data source to get started.
              </p>
            ) : (
              <div className="space-y-4">
                {credentials.map((credential) => (
                  <div
                    key={credential.id}
                    className="flex items-center justify-between rounded-lg border p-4"
                  >
                    <div>
                      <h3 className="font-medium">{credential.data_source_name}</h3>
                      <p className="text-sm text-muted-foreground">
                        Expires: {new Date(credential.token_expires_at).toLocaleDateString()}
                      </p>
                    </div>
                    <Badge variant={credential.is_valid ? "default" : "destructive"}>
                      {credential.is_valid ? "Valid" : "Expired"}
                    </Badge>
                  </div>
                ))}
              </div>
            )}
          </CardContent>
        </Card>
      </div>

      {/* Materialized Datasets */}
      {datasets.length > 0 && (
        <Card className="mt-6">
          <CardHeader>
            <CardTitle>Synced Data</CardTitle>
            <CardDescription>
              Data materialized from connected sources, available for querying.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <div className="space-y-4">
              {datasets.map((dataset) => (
                <div
                  key={dataset.id}
                  className="flex items-center justify-between rounded-lg border p-4"
                >
                  <div>
                    <h3 className="font-medium">{dataset.data_source_name}</h3>
                    <p className="text-sm text-muted-foreground">
                      Schema: {dataset.schema_name}
                      {dataset.last_sync_at && (
                        <> | Last sync: {new Date(dataset.last_sync_at).toLocaleString()}</>
                      )}
                    </p>
                    {Object.keys(dataset.row_counts).length > 0 && (
                      <p className="text-xs text-muted-foreground">
                        {Object.entries(dataset.row_counts)
                          .map(([table, count]) => `${table}: ${count} rows`)
                          .join(", ")}
                      </p>
                    )}
                  </div>
                  <div className="flex items-center gap-2">
                    {getStatusBadge(dataset.status)}
                    <Button
                      size="sm"
                      variant="outline"
                      onClick={() => handleTriggerSync(dataset.id)}
                      disabled={dataset.status === "syncing"}
                    >
                      <RefreshCw className={`h-4 w-4 ${dataset.status === "syncing" ? "animate-spin" : ""}`} />
                    </Button>
                  </div>
                </div>
              ))}
            </div>
          </CardContent>
        </Card>
      )}
    </div>
  )
}
