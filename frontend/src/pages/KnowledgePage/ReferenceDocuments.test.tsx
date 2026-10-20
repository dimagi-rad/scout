import { render, screen, within } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { afterEach, describe, expect, it, vi } from "vitest"

import { api } from "@/api/client"
import { createAppStore } from "@/store/store"
import { REFERENCE_UPLOAD_MAX_BYTES, type KnowledgeEntryItem } from "@/store/knowledgeSlice"
import { KnowledgeForm } from "./KnowledgeForm"
import { KnowledgeList } from "./KnowledgeList"
import { ReferenceUploadDialog } from "./ReferenceUploadDialog"
import { referenceFilename } from "./referenceFormat"

const note: KnowledgeEntryItem = {
  id: "note-1",
  type: "entry",
  kind: "note",
  title: "Active user",
  description: "",
  source_format: "",
  content: "A user with a visit in the last 30 days.",
  content_truncated: false,
  size: 40,
  tags: [],
  created_at: "2026-09-01T10:00:00Z",
  updated_at: "2026-09-01T10:00:00Z",
}

const registry: KnowledgeEntryItem = {
  ...note,
  id: "ref-1",
  kind: "reference",
  title: "Indicator registry",
  description: "Official data elements and indicators",
  source_format: "json",
  content: '[{"code": "IND001"}',
  content_truncated: true,
  size: 92_140,
  tags: ["registry"],
}

afterEach(() => vi.restoreAllMocks())

function renderList() {
  render(
    <KnowledgeList
      items={[note, registry]}
      filter={null}
      search=""
      onFilterChange={vi.fn()}
      onSearchChange={vi.fn()}
      onEdit={vi.fn()}
      onDelete={vi.fn()}
    />,
  )
}

describe("reference documents in the knowledge list", () => {
  it("badges reference documents with their format and size", () => {
    renderList()

    expect(screen.getByTestId("knowledge-reference-badge-ref-1")).toHaveTextContent("reference · json")
    expect(screen.getByTestId("knowledge-size-ref-1")).toHaveTextContent("92.1k chars")
    expect(screen.getByText("Official data elements and indicators")).toBeInTheDocument()
    expect(screen.queryByTestId("knowledge-reference-badge-note-1")).not.toBeInTheDocument()
  })
})

describe("reference documents in the knowledge form", () => {
  it("shows a preview and download instead of a content textarea", async () => {
    const onDownload = vi.fn().mockResolvedValue(undefined)
    render(
      <KnowledgeForm open onOpenChange={vi.fn()} item={registry} onSave={vi.fn()} onDownload={onDownload} />,
    )

    expect(screen.getByText("Edit Reference Document")).toBeInTheDocument()
    expect(screen.queryByRole("textbox", { name: "Content" })).not.toBeInTheDocument()
    expect(screen.getByTestId("knowledge-form-preview")).toHaveTextContent('[{"code": "IND001"}')
    expect(screen.getByTestId("knowledge-form-preview-note")).toHaveTextContent(
      "Preview of the first 19 of 92,140 characters.",
    )

    await userEvent.click(screen.getByTestId("knowledge-form-download"))
    expect(onDownload).toHaveBeenCalledWith(registry)
  })

  it("saves metadata without sending the previewed content back", async () => {
    const onSave = vi.fn().mockResolvedValue(undefined)
    render(<KnowledgeForm open onOpenChange={vi.fn()} item={registry} onSave={onSave} />)

    const description = screen.getByTestId("knowledge-form-description")
    await userEvent.clear(description)
    await userEvent.type(description, "Registry v2")
    await userEvent.click(screen.getByRole("button", { name: "Save Changes" }))

    expect(onSave).toHaveBeenCalledWith({
      type: "entry",
      title: "Indicator registry",
      description: "Registry v2",
      tags: ["registry"],
    })
  })

  it("still edits a note's content", async () => {
    const onSave = vi.fn().mockResolvedValue(undefined)
    render(<KnowledgeForm open onOpenChange={vi.fn()} item={note} onSave={onSave} />)

    await userEvent.click(screen.getByRole("button", { name: "Save Changes" }))

    expect(onSave).toHaveBeenCalledWith(
      expect.objectContaining({ content: note.content, title: note.title }),
    )
    expect(onSave.mock.calls[0][0]).not.toHaveProperty("description")
  })
})

describe("reference document upload", () => {
  const file = (name: string, size = 10) =>
    new File([new Uint8Array(size)], name, { type: "application/octet-stream" })

  it("defaults the title to the file name and uploads with the fields", async () => {
    const onUpload = vi.fn().mockResolvedValue(undefined)
    const onOpenChange = vi.fn()
    render(<ReferenceUploadDialog open onOpenChange={onOpenChange} onUpload={onUpload} />)

    const registryFile = file("indicator-registry.json")
    await userEvent.upload(screen.getByTestId("reference-upload-file"), registryFile)
    const title = screen.getByTestId("reference-upload-title")
    expect(title).toHaveValue("indicator-registry")
    await userEvent.clear(title)
    await userEvent.type(title, "Indicator registry")
    await userEvent.type(screen.getByTestId("reference-upload-description"), "Official list")
    await userEvent.click(screen.getByTestId("reference-upload-submit"))

    expect(onUpload).toHaveBeenCalledWith(registryFile, {
      title: "Indicator registry",
      description: "Official list",
    })
    expect(onOpenChange).toHaveBeenCalledWith(false)
  })

  it.each([
    ["registry.pdf", 10, "Upload a .json, .csv, .tsv, .md, .markdown, .txt file."],
    [
      "registry.json",
      REFERENCE_UPLOAD_MAX_BYTES + 1,
      "This file is 2,097,153 bytes; the limit is 2,097,152 bytes (2.0 MB).",
    ],
  ])("rejects %s before uploading", async (name, size, message) => {
    const onUpload = vi.fn()
    render(<ReferenceUploadDialog open onOpenChange={vi.fn()} onUpload={onUpload} />)

    // applyAccept off: the picker's accept filter is only a hint; the check must hold anyway.
    await userEvent.setup({ applyAccept: false }).upload(screen.getByTestId("reference-upload-file"), file(name, size))

    expect(screen.getByTestId("reference-upload-error")).toHaveTextContent(message)
    expect(screen.getByTestId("reference-upload-submit")).toBeDisabled()
    expect(onUpload).not.toHaveBeenCalled()
  })

  it("shows the server's reason when the upload is refused", async () => {
    const onUpload = vi.fn().mockRejectedValue(new Error("File is not valid JSON (line 1, column 9)."))
    render(<ReferenceUploadDialog open onOpenChange={vi.fn()} onUpload={onUpload} />)

    await userEvent.upload(screen.getByTestId("reference-upload-file"), file("bad.json"))
    await userEvent.click(screen.getByTestId("reference-upload-submit"))

    const dialog = screen.getByTestId("reference-upload-dialog")
    expect(await within(dialog).findByText("File is not valid JSON (line 1, column 9).")).toBeInTheDocument()
  })
})

describe("reference document store actions", () => {
  it("uploads to the workspace's upload endpoint and adds the document to the list", async () => {
    const store = createAppStore()
    store.getState().domainActions.setActiveDomain("ws-1")
    const upload = vi.spyOn(api, "upload").mockResolvedValue(registry)
    const doc = file("registry.json")

    await store.getState().knowledgeActions.uploadReferenceDocument(doc, { title: "Registry" })

    const [url, body] = upload.mock.calls[0]
    expect(url).toBe("/api/workspaces/ws-1/knowledge/upload/")
    expect((body as FormData).get("file")).toBe(doc)
    expect((body as FormData).get("title")).toBe("Registry")
    expect((body as FormData).has("description")).toBe(false)
    expect(store.getState().knowledgeItems).toEqual([registry])
  })

  function file(name: string) {
    return new File(["[]"], name)
  }
})

describe("referenceFilename", () => {
  it("keeps the format's extension and strips unsafe characters", () => {
    expect(referenceFilename({ title: "Registry: v2/final", source_format: "json" })).toBe(
      "Registry_ v2_final.json",
    )
    expect(referenceFilename({ title: "Notes", source_format: "" })).toBe("Notes.txt")
    expect(referenceFilename({ title: "指标登记表", source_format: "json" })).toBe("指标登记表.json")
    expect(referenceFilename({ title: "***", source_format: "csv" })).toBe("document.csv")
  })
})
