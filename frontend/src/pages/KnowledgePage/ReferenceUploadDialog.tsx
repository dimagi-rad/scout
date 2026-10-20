import { useEffect, useState } from "react"
import { Loader2 } from "lucide-react"
import { useIsCurrentAccount } from "@/hooks/useIsCurrentAccount"
import { writeErrorMessage } from "@/hooks/useWorkspaceRole"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import {
  REFERENCE_UPLOAD_EXTENSIONS,
  REFERENCE_UPLOAD_MAX_BYTES,
  type ReferenceUploadFields,
} from "@/store/knowledgeSlice"
import { fileProblem, formatSize } from "./referenceFormat"

interface ReferenceUploadDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  onUpload: (file: File, fields: ReferenceUploadFields) => Promise<unknown>
}

function titleFromFilename(name: string): string {
  const dot = name.lastIndexOf(".")
  return (dot > 0 ? name.slice(0, dot) : name).trim()
}

export function ReferenceUploadDialog({ open, onOpenChange, onUpload }: ReferenceUploadDialogProps) {
  const isCurrentAccount = useIsCurrentAccount()
  const [file, setFile] = useState<File | null>(null)
  const [title, setTitle] = useState("")
  const [description, setDescription] = useState("")
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!open) return
    setFile(null)
    setTitle("")
    setDescription("")
    setError(null)
  }, [open])

  const handleFile = (e: React.ChangeEvent<HTMLInputElement>) => {
    const picked = e.target.files?.[0] ?? null
    setFile(picked)
    setError(picked ? fileProblem(picked) : null)
    if (picked) setTitle(titleFromFilename(picked.name))
  }

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!file || fileProblem(file)) return
    setLoading(true)
    setError(null)
    try {
      await onUpload(file, { title: title.trim(), description: description.trim() })
      if (!isCurrentAccount()) return
      onOpenChange(false)
    } catch (err) {
      if (!isCurrentAccount()) return
      const fallback = err instanceof Error ? err.message : "Upload failed. Try again."
      setError(writeErrorMessage(err, fallback, true))
    } finally {
      if (isCurrentAccount()) setLoading(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={(next) => !loading && onOpenChange(next)}>
      <DialogContent className="max-w-lg" data-testid="reference-upload-dialog">
        <DialogHeader>
          <DialogTitle>Upload Reference Document</DialogTitle>
          <DialogDescription>
            Share a large file, such as an indicator registry or code list, with every
            conversation in this workspace. Scout lists it in every chat and reads or searches
            it when a question needs it. JSON, CSV, TSV, Markdown or text, UTF-8, up to{" "}
            {formatSize(REFERENCE_UPLOAD_MAX_BYTES)}.
          </DialogDescription>
        </DialogHeader>

        <form onSubmit={handleSubmit}>
          {error && (
            <div
              className="mb-4 rounded-md bg-destructive/10 p-3 text-sm text-destructive"
              role="alert"
              data-testid="reference-upload-error"
            >
              {error}
            </div>
          )}
          <div className="space-y-4">
            <div className="space-y-2">
              <Label htmlFor="reference-file">File</Label>
              <Input
                id="reference-file"
                type="file"
                accept={REFERENCE_UPLOAD_EXTENSIONS.join(",")}
                onChange={handleFile}
                data-testid="reference-upload-file"
              />
              {file && !fileProblem(file) && (
                <p className="text-xs text-muted-foreground" data-testid="reference-upload-size">
                  {formatSize(file.size)}
                </p>
              )}
            </div>
            <div className="space-y-2">
              <Label htmlFor="reference-title">Title</Label>
              <Input
                id="reference-title"
                value={title}
                onChange={(e) => setTitle(e.target.value)}
                placeholder="Defaults to the file name"
                maxLength={255}
                data-testid="reference-upload-title"
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="reference-description">Description</Label>
              <Input
                id="reference-description"
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                placeholder="What it is and when Scout should use it (optional)"
                maxLength={500}
                data-testid="reference-upload-description"
              />
            </div>
          </div>
          <DialogFooter className="mt-6">
            <Button
              type="button"
              variant="outline"
              onClick={() => onOpenChange(false)}
              disabled={loading}
            >
              Cancel
            </Button>
            <Button
              type="submit"
              disabled={loading || !file || !!fileProblem(file)}
              data-testid="reference-upload-submit"
            >
              {loading && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
              Upload
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
