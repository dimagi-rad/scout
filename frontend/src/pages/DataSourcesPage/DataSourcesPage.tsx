import { Database } from "lucide-react"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"

export function DataSourcesPage() {
  return (
    <div className="container mx-auto py-8">
      <div className="mb-6">
        <h1 className="text-2xl font-bold">Data Sources</h1>
        <p className="text-muted-foreground">
          Manage data sources connected to your workspace.
        </p>
      </div>

      <Card data-testid="data-sources-card">
        <CardHeader>
          <CardTitle className="flex items-center">
            <Database className="mr-2 h-5 w-5" />
            CommCare Data Sources
          </CardTitle>
          <CardDescription>
            Data is loaded from CommCare via tenant credentials. Use the chat interface to run
            materialization and query your data.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <p className="text-sm text-muted-foreground">
            To load data, select a CommCare domain from the sidebar and ask the agent to
            &ldquo;load my cases&rdquo; or &ldquo;run materialization&rdquo;.
          </p>
        </CardContent>
      </Card>
    </div>
  )
}
