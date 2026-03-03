import { useState } from "react"
import { useNavigate, useLocation } from "react-router-dom"
import { useAppStore } from "@/store/store"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Button } from "@/components/ui/button"

export function ConnectionPromptDialog() {
  const [dismissed, setDismissed] = useState(false)
  const navigate = useNavigate()
  const location = useLocation()
  const domainsStatus = useAppStore((s) => s.domainsStatus)
  const domains = useAppStore((s) => s.domains)

  const onConnectionsPage = location.pathname === "/settings/connections"
  const open =
    domainsStatus === "loaded" && domains.length === 0 && !dismissed && !onConnectionsPage

  return (
    <Dialog open={open} onOpenChange={(v) => !v && setDismissed(true)}>
      <DialogContent data-testid="connection-prompt-dialog">
        <DialogHeader>
          <DialogTitle>Connect a data source</DialogTitle>
          <DialogDescription>
            You don't have any data sources connected yet. Visit the Connections
            page to link a CommCare account, Connect server, or custom workspace.
          </DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button
            variant="outline"
            data-testid="connection-prompt-later"
            onClick={() => setDismissed(true)}
          >
            Later
          </Button>
          <Button
            data-testid="connection-prompt-go"
            onClick={() => {
              setDismissed(true)
              navigate("/settings/connections")
            }}
          >
            Go to Connections
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
