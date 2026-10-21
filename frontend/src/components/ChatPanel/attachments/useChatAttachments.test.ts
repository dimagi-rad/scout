import { act, renderHook, waitFor } from "@testing-library/react"
import { beforeEach, describe, expect, it, vi } from "vitest"

import { uploadChatAttachment } from "@/api/chatAttachments"
import { useChatAttachments } from "./useChatAttachments"

vi.mock("@/api/chatAttachments", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/chatAttachments")>()),
  uploadChatAttachment: vi.fn(),
}))

const upload = vi.mocked(uploadChatAttachment)

function csv(name: string) {
  return new File(["a,b"], name, { type: "text/csv" })
}

function part(i: number) {
  return {
    type: "file" as const,
    mediaType: "text/csv",
    filename: `sent-${i}.csv`,
    url: `/api/chat/attachments/${String(i).padStart(8, "0")}-0000-0000-0000-000000000000/`,
  }
}

beforeEach(() => {
  upload.mockReset()
  upload.mockImplementation(async (file) => ({
    id: file.name,
    filename: file.name,
    mediaType: file.type,
    size: file.size,
    url: `/api/chat/attachments/${file.name}/`,
  }))
})

describe("useChatAttachments", () => {
  it("keeps files added since a refused send, and says what did not fit back", async () => {
    const { result } = renderHook(() => useChatAttachments("ws", "t1"))
    act(() => result.current.add(Array.from({ length: 4 }, (_, i) => csv(`new-${i}.csv`))))
    await waitFor(() => expect(result.current.items.every((i) => i.status === "ready")).toBe(true))

    act(() => result.current.restore("ws", "t1", Array.from({ length: 8 }, (_, i) => part(i))))

    expect(result.current.items.map((i) => i.filename)).toEqual([
      "new-0.csv", "new-1.csv", "new-2.csv", "new-3.csv",
      "sent-0.csv", "sent-1.csv", "sent-2.csv", "sent-3.csv", "sent-4.csv", "sent-5.csv",
    ])
    expect(result.current.notice).toMatch(/2 of the files .* didn't fit back/)
  })

  it("counts a removal before an add in the same tick", () => {
    const { result } = renderHook(() => useChatAttachments("ws", "t1"))
    act(() => result.current.add(Array.from({ length: 10 }, (_, i) => csv(`f-${i}.csv`))))
    act(() => {
      result.current.remove(result.current.items[0].localId)
      result.current.add([csv("late.csv")])
    })
    expect(result.current.items).toHaveLength(10)
    expect(result.current.notice).toBeNull()
  })

  it("keeps one chat's notice when a chip is removed in another", () => {
    const { result, rerender } = renderHook(({ thread }) => useChatAttachments("ws", thread), {
      initialProps: { thread: "a" },
    })
    act(() => result.current.add(Array.from({ length: 11 }, (_, i) => csv(`f-${i}.csv`))))
    expect(result.current.notice).toMatch(/up to 10 files/)
    rerender({ thread: "b" })
    act(() => result.current.add([csv("b.csv")]))
    act(() => result.current.remove(result.current.items[0].localId))
    rerender({ thread: "a" })
    expect(result.current.notice).toMatch(/up to 10 files/)
  })
})
