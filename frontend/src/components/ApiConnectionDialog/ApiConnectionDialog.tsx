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

export interface ConnectionChatbot {
  membership_id: string
  tenant_id: string
  tenant_name: string
  team_slug: string
  team_name: string
}

export interface ApiKeyConnection {
  connection_id: string
  provider: string
  credential_type: string
  /**
   * The scope this credential authorises: an OCS team slug, or a CommCare HQ
   * server ("" = www, "eu" = EU).
   */
  scope_key: string
  scope_label: string
  /**
   * Per-connection token health. null for API keys, which have no expiry.
   * needs_team: a team-less OCS sign-in that can reach no data (#379).
   */
  status: "connected" | "expired" | "needs_team" | null
  /**
   * Whether the connection still reaches its sources. expired: the sign-in or key
   * is dead, so reconnecting fixes it. refused: the provider refuses a working
   * sign-in, so an admin there must restore access. partial: some sources lost.
   */
  access_state?: "ok" | "expired" | "refused" | "partial"
  denial_code?: string | null
  denied_at?: string | null
  chatbots: ConnectionChatbot[]
  /** Sources this connection no longer reaches (archived memberships). */
  archived_chatbots?: (ConnectionChatbot & { archived_at: string })[]
}

interface FieldOption {
  value: string
  label: string
}

interface ProviderField {
  key: string
  label: string
  type: "text" | "password" | "select"
  required: boolean
  editable_on_rotate: boolean
  /** For "select": the choices, the first being the default. */
  options?: FieldOption[]
}

const SELECT_CLASSES =
  "flex h-9 w-full rounded-md border border-input bg-background px-3 py-1 text-sm text-foreground shadow-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"

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

/** A select's shown default is submitted too, so the payload matches what the user saw. */
function initialValues(schema: ProviderSchema | undefined, mode: "add" | "edit") {
  const values: Record<string, string> = {}
  if (mode !== "add") return values
  for (const f of schema?.fields ?? []) {
    if (f.type === "select" && f.options?.length) values[f.key] = f.options[0].value
  }
  return values
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
        setValues(initialValues(data.find((s) => s.id === initial), mode))
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
          `/api/auth/connections/${editing.connection_id}/`,
          { fields: values },
        )
      } else {
        await api.post<{ memberships: MembershipResult[] }>(
          "/api/auth/connections/",
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
                      onChange={() => {
                        // A key typed for one provider must not be posted to another.
                        setProviderId(s.id)
                        setValues(initialValues(s, mode))
                      }}
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
              {f.type === "select" ? (
                <select
                  id={`field-${f.key}`}
                  className={SELECT_CLASSES}
                  value={values[f.key] ?? ""}
                  onChange={(e) =>
                    setValues((prev) => ({ ...prev, [f.key]: e.target.value }))
                  }
                  data-testid={`api-connection-field-${f.key}`}
                >
                  {f.options?.map((option) => (
                    <option key={option.value} value={option.value}>
                      {option.label}
                    </option>
                  ))}
                </select>
              ) : (
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
              )}
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
