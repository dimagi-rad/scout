import { useState, useEffect, type FormEvent } from "react"
import { api } from "@/api/client"
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Button } from "@/components/ui/button"

export interface ApiKeyConnection {
  membership_id: string
  provider: string
  tenant_id: string
  tenant_name: string
  credential_type: string
}

interface ProviderField {
  key: string
  label: string
  type: "text" | "password"
  required: boolean
  editable_on_rotate: boolean
}

interface ProviderSchema {
  id: string
  display_name: string
  fields: ProviderField[]
}

interface MembershipResult {
  membership_id: string
  tenant_id: string
  tenant_name: string
}

interface Props {
  open: boolean
  mode: "add" | "edit"
  editing: ApiKeyConnection | null
  onClose: () => void
  onSaved: () => void | Promise<void>
}

export function ApiConnectionDialog({ open, mode, editing, onClose, onSaved }: Props) {
  const [schemas, setSchemas] = useState<ProviderSchema[]>([])
  const [providerId, setProviderId] = useState<string>("")
  const [values, setValues] = useState<Record<string, string>>({})
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!open) return
    api
      .get<ProviderSchema[]>("/api/auth/api-key-providers/")
      .then((data) => {
        setSchemas(data)
        const initial =
          mode === "edit" && editing ? editing.provider : (data[0]?.id ?? "")
        setProviderId(initial)
        setValues({})
        setError(null)
      })
      .catch(() => setError("Failed to load provider list."))
  }, [open, mode, editing])

  const schema = schemas.find((s) => s.id === providerId) ?? null
  const visibleFields =
    schema?.fields.filter((f) => (mode === "edit" ? f.editable_on_rotate : true)) ?? []

  async function handleSubmit(e: FormEvent) {
    e.preventDefault()
    if (!schema) return
    setLoading(true)
    setError(null)
    try {
      if (mode === "edit" && editing) {
        await api.patch(
          `/api/auth/tenant-credentials/${editing.membership_id}/`,
          { fields: values },
        )
      } else {
        await api.post<{ memberships: MembershipResult[] }>(
          "/api/auth/tenant-credentials/",
          { provider: providerId, fields: values },
        )
      }
      await onSaved()
      onClose()
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to save connection.")
    } finally {
      setLoading(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={(v) => (v ? null : onClose())}>
      <DialogContent data-testid="api-connection-dialog">
        <DialogHeader>
          <DialogTitle>
            {mode === "edit" ? "Edit API connection" : "Add API connection"}
          </DialogTitle>
          <DialogDescription>
            {mode === "edit"
              ? "Rotate the API key for this connection."
              : "Connect a provider with a personal API key."}
          </DialogDescription>
        </DialogHeader>

        <form onSubmit={handleSubmit} className="space-y-4">
          {mode === "add" && schemas.length > 1 && (
            <div className="space-y-2">
              <Label>Provider</Label>
              <div className="flex flex-col gap-2">
                {schemas.map((s) => (
                  <label
                    key={s.id}
                    htmlFor={`provider-${s.id}`}
                    className="flex items-center gap-2 cursor-pointer"
                  >
                    <input
                      type="radio"
                      id={`provider-${s.id}`}
                      name="api-connection-provider"
                      value={s.id}
                      checked={providerId === s.id}
                      onChange={() => setProviderId(s.id)}
                      data-testid={`api-connection-provider-${s.id}`}
                    />
                    <span>{s.display_name}</span>
                  </label>
                ))}
              </div>
            </div>
          )}

          {visibleFields.map((f) => (
            <div key={f.key} className="space-y-2">
              <Label htmlFor={`field-${f.key}`}>{f.label}</Label>
              <Input
                id={`field-${f.key}`}
                type={f.type}
                required={f.required}
                value={values[f.key] ?? ""}
                onChange={(e) =>
                  setValues((prev) => ({ ...prev, [f.key]: e.target.value }))
                }
                data-testid={`api-connection-field-${f.key}`}
              />
            </div>
          ))}

          {error && (
            <p className="text-sm text-destructive" role="alert">
              {error}
            </p>
          )}

          <DialogFooter>
            <Button type="button" variant="outline" onClick={onClose}>
              Cancel
            </Button>
            <Button
              type="submit"
              disabled={loading || !schema}
              data-testid="api-connection-submit"
            >
              {loading ? "Saving..." : "Save"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
