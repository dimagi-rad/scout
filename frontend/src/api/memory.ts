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
