import { Component, type ReactNode } from "react"
import { AlertTriangle, RefreshCw } from "lucide-react"
import { Button } from "@/components/ui/button"
import { reportRenderError } from "@/lib/reportRenderError"

interface Props {
  children: ReactNode
  fallback?: ReactNode
  artifactId?: string
  artifactVersion?: number
  /** Clears a caught error when it changes, e.g. after the data is refreshed. */
  resetKey?: string
}

interface State {
  hasError: boolean
  error: Error | null
}

/**
 * Error boundary component that catches JavaScript errors in child components.
 * Displays a fallback UI instead of crashing the entire app.
 */
export class ErrorBoundary extends Component<Props, State> {
  constructor(props: Props) {
    super(props)
    this.state = { hasError: false, error: null }
  }

  static getDerivedStateFromError(error: Error): State {
    return { hasError: true, error }
  }

  componentDidCatch(thrown: unknown, errorInfo: React.ErrorInfo) {
    console.error("ErrorBoundary caught an error:", thrown, errorInfo)
    // React hands over whatever was thrown. Reading fields off null would throw
    // here and lose the report, and a non-Error may be app data, so only its
    // type is sent, as the artifact sandbox does.
    const error = thrown instanceof Error ? thrown : undefined
    const nonErrorMessage =
      typeof thrown === "string"
        ? thrown
        : `Non-Error exception (${thrown === null ? "null" : typeof thrown})`
    // React 19 does not rethrow errors a boundary catches, so Sentry's global
    // handlers never see them.
    reportRenderError({
      source: "boundary",
      name: error?.name ?? "Error",
      message: error ? error.message : nonErrorMessage,
      stack: error?.stack,
      artifactId: this.props.artifactId,
      artifactVersion: this.props.artifactVersion,
    })
  }

  componentDidUpdate(prevProps: Props) {
    if (this.state.hasError && prevProps.resetKey !== this.props.resetKey) {
      this.handleReset()
    }
  }

  handleReset = () => {
    this.setState({ hasError: false, error: null })
  }

  render() {
    if (this.state.hasError) {
      if (this.props.fallback) {
        return this.props.fallback
      }

      return (
        <div className="flex min-h-[400px] flex-col items-center justify-center p-8">
          <div className="mx-auto max-w-md text-center">
            <div className="mb-4 flex justify-center">
              <div className="rounded-full bg-destructive/10 p-3">
                <AlertTriangle className="h-8 w-8 text-destructive" />
              </div>
            </div>
            <h2 className="mb-2 text-xl font-semibold">Something went wrong</h2>
            <p className="mb-4 text-sm text-muted-foreground">
              An unexpected error occurred. Please try refreshing the page.
            </p>
            {this.state.error && (
              <details className="mb-4 rounded-md border bg-muted/50 p-3 text-left">
                <summary className="cursor-pointer text-sm font-medium">
                  Error details
                </summary>
                <pre className="mt-2 overflow-auto text-xs text-destructive">
                  {this.state.error.message}
                </pre>
              </details>
            )}
            <div className="flex justify-center gap-2">
              <Button variant="outline" onClick={this.handleReset}>
                <RefreshCw className="mr-2 h-4 w-4" />
                Try again
              </Button>
              <Button onClick={() => window.location.reload()}>
                Reload page
              </Button>
            </div>
          </div>
        </div>
      )
    }

    return this.props.children
  }
}
