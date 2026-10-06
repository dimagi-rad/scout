import { useEffect, useState } from "react"
import { Loader2, Plus, User } from "lucide-react"
import { personalMemoryApi, type PersonalMemory } from "@/api/memory"
import { useAppStore } from "@/store/store"
import { useIsCurrentAccount } from "@/hooks/useIsCurrentAccount"
import { Button } from "@/components/ui/button"
import { Textarea } from "@/components/ui/textarea"
import { MemoryItemList } from "./MemoryItemList"
import { errorText } from "./errorText"

type LoadStatus = "loading" | "loaded" | "error"

function PersonalMemorySection() {
  const userId = useAppStore((s) => s.user?.id)
  const isCurrentAccount = useIsCurrentAccount()
  const [memories, setMemories] = useState<PersonalMemory[]>([])
  const [status, setStatus] = useState<LoadStatus>("loading")
  const [draft, setDraft] = useState("")
  const [adding, setAdding] = useState(false)
  const [addError, setAddError] = useState<string | null>(null)

  useEffect(() => {
    const controller = new AbortController()
    setStatus("loading")
    personalMemoryApi
      .list(controller.signal)
      .then((data) => {
        setMemories(data.results)
        setStatus("loaded")
      })
      .catch(() => {
        if (!controller.signal.aborted) setStatus("error")
      })
    return () => controller.abort()
  }, [userId])

  const add = async () => {
    if (!draft.trim() || adding) return
    setAdding(true)
    setAddError(null)
    try {
      const saved = await personalMemoryApi.create(draft)
      if (!isCurrentAccount()) return
      setMemories((current) =>
        current.some((m) => m.id === saved.id) ? current : [...current, saved],
      )
      setDraft("")
    } catch (error) {
      if (isCurrentAccount()) setAddError(errorText(error, "Couldn’t save this memory. Try again."))
    } finally {
      if (isCurrentAccount()) setAdding(false)
    }
  }

  const update = async (id: string, content: string) => {
    const saved = await personalMemoryApi.update(id, content)
    if (isCurrentAccount()) setMemories((current) => current.map((m) => (m.id === id ? saved : m)))
  }

  const remove = async (id: string) => {
    await personalMemoryApi.remove(id)
    if (isCurrentAccount()) setMemories((current) => current.filter((m) => m.id !== id))
  }

  return (
    <section className="space-y-3" data-testid="memory-personal-section">
      <div className="flex items-center gap-2">
        <User className="h-4 w-4 text-muted-foreground" />
        <h2 className="text-lg font-semibold">Personal</h2>
      </div>
      <p className="text-sm text-muted-foreground">
        Your formats, habits and preferences. Scout applies them in every workspace, and only you
        can see them.
      </p>

      {status === "loading" && (
        <p className="text-sm text-muted-foreground" data-testid="memory-personal-loading">
          Loading memories…
        </p>
      )}
      {status === "error" && (
        <p className="text-sm text-destructive" data-testid="memory-personal-error">
          Couldn’t load your memories. Try again.
        </p>
      )}
      {status === "loaded" && (
        <MemoryItemList
          testIdPrefix="memory-personal"
          items={memories.map((m) => ({ id: m.id, content: m.content, canEdit: true }))}
          emptyText="Nothing saved yet. Ask Scout to remember a preference, or add one below."
          onUpdate={update}
          onDelete={remove}
        />
      )}

      <div className="space-y-2">
        <Textarea
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder="e.g. Show district totals as a table, sorted by district name."
          aria-label="New personal memory"
          data-testid="memory-personal-add-input"
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
          data-testid="memory-personal-add"
        >
          {adding ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Plus className="mr-2 h-4 w-4" />}
          Add memory
        </Button>
      </div>
    </section>
  )
}

export function MemoryPage() {
  return (
    <div className="p-6">
      <div className="mb-6">
        <h1 className="text-2xl font-bold">Memory</h1>
        <p className="text-muted-foreground">
          What Scout remembers between conversations. Edit or delete anything here.
        </p>
      </div>
      <div className="space-y-10">
        <PersonalMemorySection />
      </div>
    </div>
  )
}
