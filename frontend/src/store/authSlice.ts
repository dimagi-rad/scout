import type { StateCreator } from "zustand"
import { api, ApiError } from "@/api/client"

export type AuthStatus = "idle" | "loading" | "authenticated" | "unauthenticated"

export interface User {
  id: string
  email: string
  name: string
  is_staff: boolean
}

export interface AuthSlice {
  user: User | null
  authStatus: AuthStatus
  authError: string | null
  authActions: {
    fetchMe: () => Promise<void>
    login: (email: string, password: string) => Promise<void>
    logout: () => Promise<void>
  }
}

export const createAuthSlice: StateCreator<AuthSlice, [], [], AuthSlice> = (set) => ({
  user: null,
  authStatus: "idle",
  authError: null,
  authActions: {
    fetchMe: async () => {
      set({ authStatus: "loading", authError: null })
      try {
        // Ensure CSRF cookie is set
        await api.get("/api/auth/csrf/")
        const user = await api.get<User>("/api/auth/me/")
        set({ user, authStatus: "authenticated" })
      } catch (e) {
        if (e instanceof ApiError && e.status === 401) {
          set({ user: null, authStatus: "unauthenticated" })
        } else {
          set({ user: null, authStatus: "unauthenticated", authError: "Failed to check auth" })
        }
      }
    },

    login: async (email: string, password: string) => {
      set({ authStatus: "loading", authError: null })
      try {
        // Refresh CSRF cookie before login to avoid stale token (e.g. from admin session)
        await api.get("/api/auth/csrf/")
        const user = await api.post<User>("/api/auth/login/", { email, password })
        set({ user, authStatus: "authenticated", authError: null })
      } catch (e) {
        const message = e instanceof ApiError ? e.message : "Login failed"
        set({ authStatus: "unauthenticated", authError: message })
        throw e
      }
    },

    logout: async () => {
      try {
        await api.post("/api/auth/logout/")
      } finally {
        set({ user: null, authStatus: "unauthenticated", authError: null })
      }
    },
  },
})
