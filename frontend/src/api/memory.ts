import { api } from "./client"

export interface PersonalMemory {
  id: string
  content: string
  created_at: string
  updated_at: string
}

interface PersonalMemoryList {
  results: PersonalMemory[]
  limit: number
}

const PERSONAL_URL = "/api/memory/personal/"

export const personalMemoryApi = {
  list: (signal?: AbortSignal) => api.get<PersonalMemoryList>(PERSONAL_URL, signal),
  create: (content: string) => api.post<PersonalMemory>(PERSONAL_URL, { content }),
  update: (id: string, content: string) =>
    api.patch<PersonalMemory>(`${PERSONAL_URL}${id}/`, { content }),
  remove: (id: string) => api.delete<void>(`${PERSONAL_URL}${id}/`),
}

export interface WorkspaceMemory {
  id: string
  content: string
  tables: string[]
  author_name: string
  is_mine: boolean
  can_edit: boolean
  created_at: string
  updated_at: string
}

interface WorkspaceMemoryList {
  results: WorkspaceMemory[]
  can_add: boolean
}

const workspaceUrl = (workspaceId: string) => `/api/workspaces/${workspaceId}/memory/`

export const workspaceMemoryApi = {
  list: (workspaceId: string, signal?: AbortSignal) =>
    api.get<WorkspaceMemoryList>(workspaceUrl(workspaceId), signal),
  create: (workspaceId: string, content: string) =>
    api.post<WorkspaceMemory>(workspaceUrl(workspaceId), { content }),
  update: (workspaceId: string, id: string, content: string) =>
    api.patch<WorkspaceMemory>(`${workspaceUrl(workspaceId)}${id}/`, { content }),
  remove: (workspaceId: string, id: string) =>
    api.delete<void>(`${workspaceUrl(workspaceId)}${id}/`),
}
