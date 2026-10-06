import { useEffect, useRef, useState, type ReactNode } from "react"
import { Loader2, Plus, User, Users } from "lucide-react"
import {
  personalMemoryApi,
  workspaceMemoryApi,
  type PersonalMemory,
  type WorkspaceMemory,
} from "@/api/memory"
import { useAppStore } from "@/store/store"
import { useIsCurrentAccount } from "@/hooks/useIsCurrentAccount"
import { Button } from "@/components/ui/button"
import { Textarea } from "@/components/ui/textarea"
import { MemoryItemList, type MemoryListItem } from "./MemoryItemList"
import { errorText } from "./errorText"

type LoadStatus = "loading" | "loaded" | "error"

interface MemorySectionProps<T extends { id: string }> {
  layer: "personal" | "workspace"
  icon: ReactNode
  title: string
  description: string
  emptyText: string
  placeholder: string
  // Changing it reloads the list (another account or another workspace).
  scopeKey: string | undefined
  load: (signal: AbortSignal) => Promise<{ results: T[]; canAdd: boolean }>
  create: (content: string) => Promise<T>
  update: (id: string, content: string) => Promise<T>
  remove: (id: string) => Promise<void>
  toListItem: (memory: T) => MemoryListItem
  readOnlyNote?: string
  // Matches the server's list order, so a new memory doesn't jump on reload.
  newestFirst: boolean
}

function MemorySection<T extends { id: string }>({
  layer,
  icon,
  title,
  description,
  emptyText,
  placeholder,
  scopeKey,
  load,
  create,
  update,
  remove,
  toListItem,
  readOnlyNote,
  newestFirst,
}: MemorySectionProps<T>) {
  const isCurrentAccount = useIsCurrentAccount()
  const [memories, setMemories] = useState<T[]>([])
  const [canAdd, setCanAdd] = useState(false)
  const [status, setStatus] = useState<LoadStatus>("loading")
  const [loadAttempt, setLoadAttempt] = useState(0)
  const [draft, setDraft] = useState("")
  const [adding, setAdding] = useState(false)
  const [addError, setAddError] = useState<string | null>(null)
  const prefix = `memory-${layer}`
  const scopeRef = useRef(scopeKey)
  useEffect(() => {
    scopeRef.current = scopeKey
  }, [scopeKey])
  // A response for another account or workspace must not land in this list.
  const stillCurrent = (startedFor: string | undefined) =>
    isCurrentAccount() && scopeRef.current === startedFor

  useEffect(() => {
    const controller = new AbortController()
    setStatus("loading")
    setMemories([])
    setDraft("")
    setAddError(null)
    load(controller.signal)
      .then((data) => {
        if (controller.signal.aborted) return
        setMemories(data.results)
        setCanAdd(data.canAdd)
        setStatus("loaded")
      })
      .catch(() => {
        if (!controller.signal.aborted) setStatus("error")
      })
    return () => controller.abort()
    // load closes over scopeKey; the key is what decides a reload.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scopeKey, loadAttempt])

  const add = async () => {
    if (!draft.trim() || adding) return
    const startedFor = scopeKey
    setAdding(true)
    setAddError(null)
    try {
      const saved = await create(draft)
      if (!stillCurrent(startedFor)) return
      if (memories.some((m) => m.id === saved.id)) {
        setAddError("That memory is already saved.")
        return
      }
      setMemories((current) => {
        const rest = current.filter((m) => m.id !== saved.id)
        return newestFirst ? [saved, ...rest] : [...rest, saved]
      })
      setDraft("")
    } catch (error) {
      if (stillCurrent(startedFor)) {
        setAddError(errorText(error, "Couldn’t save this memory. Try again."))
      }
    } finally {
      if (isCurrentAccount()) setAdding(false)
    }
  }

  const handleUpdate = async (id: string, content: string) => {
    const startedFor = scopeKey
    const saved = await update(id, content)
    if (stillCurrent(startedFor)) {
      setMemories((current) => current.map((m) => (m.id === id ? saved : m)))
    }
  }

  const handleRemove = async (id: string) => {
    const startedFor = scopeKey
    await remove(id)
    if (stillCurrent(startedFor)) setMemories((current) => current.filter((m) => m.id !== id))
  }

  return (
    <section className="space-y-3" data-testid={`${prefix}-section`}>
      <div className="flex items-center gap-2">
        {icon}
        <h2 className="text-lg font-semibold">{title}</h2>
      </div>
      <p className="text-sm text-muted-foreground">{description}</p>

      {status === "loading" && (
        <p className="text-sm text-muted-foreground" data-testid={`${prefix}-loading`}>
          Loading memories…
        </p>
      )}
      {status === "error" && (
        <div className="flex items-center gap-3">
          <p className="text-sm text-destructive" data-testid={`${prefix}-error`}>
            Couldn’t load these memories.
          </p>
          <Button
            size="sm"
            variant="outline"
            onClick={() => setLoadAttempt((n) => n + 1)}
            data-testid={`${prefix}-retry`}
          >
            Try again
          </Button>
        </div>
      )}
      {status === "loaded" && (
        <>
          <MemoryItemList
            testIdPrefix={prefix}
            items={memories.map(toListItem)}
            emptyText={emptyText}
            onUpdate={handleUpdate}
            onDelete={handleRemove}
          />
          {canAdd ? (
            <div className="space-y-2">
              <Textarea
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                placeholder={placeholder}
                aria-label={`New ${layer} memory`}
                data-testid={`${prefix}-add-input`}
              />
              {addError && (
                <p className="text-sm text-destructive" role="alert">
                  {addError}
                </p>
              )}
              <Button
                size="sm"
                onClick={add}
                disabled={adding || !draft.trim()}
                data-testid={`${prefix}-add`}
              >
                {adding ? (
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                ) : (
                  <Plus className="mr-2 h-4 w-4" />
                )}
                Add memory
              </Button>
            </div>
          ) : (
            readOnlyNote && (
              <p className="text-sm text-muted-foreground" data-testid={`${prefix}-read-only`}>
                {readOnlyNote}
              </p>
            )
          )}
        </>
      )}
    </section>
  )
}

function formatDate(iso: string): string {
  return new Date(iso).toLocaleDateString()
}

export function MemoryPage() {
  const userId = useAppStore((s) => s.user?.id)
  const workspaceId = useAppStore((s) => s.activeDomainId)
  const workspaceName = useAppStore(
    (s) => s.domains.find((d) => d.id === s.activeDomainId)?.name,
  )

  return (
    <div className="p-6">
      <div className="mb-6">
        <h1 className="text-2xl font-bold">Memory</h1>
        <p className="text-muted-foreground">
          What Scout remembers between conversations. Edit or delete anything here.
        </p>
      </div>
      <div className="space-y-10">
        <MemorySection<PersonalMemory>
          layer="personal"
          newestFirst={false}
          icon={<User className="h-4 w-4 text-muted-foreground" />}
          title="Personal"
          description="Your formats, habits and preferences. Scout applies them in every workspace, and only you can see them."
          emptyText="Nothing saved yet. Ask Scout to remember a preference, or add one below."
          placeholder="e.g. Show district totals as a table, sorted by district name."
          scopeKey={userId}
          load={async (signal) => {
            const data = await personalMemoryApi.list(signal)
            return { results: data.results, canAdd: true }
          }}
          create={personalMemoryApi.create}
          update={personalMemoryApi.update}
          remove={personalMemoryApi.remove}
          toListItem={(m) => ({ id: m.id, content: m.content, canEdit: true })}
        />
        {workspaceId && (
          <MemorySection<WorkspaceMemory>
            layer="workspace"
            newestFirst
            icon={<Users className="h-4 w-4 text-muted-foreground" />}
            title={workspaceName ? `Workspace: ${workspaceName}` : "Workspace"}
            description="How this workspace's data should be combined or interpreted. Shared with every member and applied in all of their chats here."
            emptyText="Nothing saved for this workspace yet."
            placeholder="e.g. Visits with status 'test' are training data; exclude them from counts."
            scopeKey={`${userId}:${workspaceId}`}
            load={async (signal) => {
              const data = await workspaceMemoryApi.list(workspaceId, signal)
              return { results: data.results, canAdd: data.can_add }
            }}
            create={(content) => workspaceMemoryApi.create(workspaceId, content)}
            update={(id, content) => workspaceMemoryApi.update(workspaceId, id, content)}
            remove={(id) => workspaceMemoryApi.remove(workspaceId, id)}
            toListItem={(m) => ({
              id: m.id,
              content: m.content,
              canEdit: m.can_edit,
              meta: `${m.is_mine ? "You" : m.author_name} · ${formatDate(m.created_at)}${
                m.tables.length ? ` · ${m.tables.join(", ")}` : ""
              }`,
            })}
            readOnlyNote="Your role in this workspace is read-only. A member with read-write access can add workspace memories."
          />
        )}
      </div>
    </div>
  )
}
