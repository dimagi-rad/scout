import { api } from "@/api/client"

export type { Thread } from "@/store/uiSlice"

export async function markThreadViewed(workspaceId: string, threadId: string): Promise<void> {
  return api.post<void>(`/api/workspaces/${workspaceId}/threads/${threadId}/viewed/`, {})
}
