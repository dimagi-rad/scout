import type { StateCreator } from "zustand"
import { api, ApiError } from "@/api/client"
import { clearUserTenantsCache } from "@/api/userTenantsCache"

export type AuthStatus = "idle" | "loading" | "authenticated" | "unauthenticated"

export interface User {
  id: string
  email: string
  name: string
  is_staff: boolean
  // Absent on older servers. Only hides the nav link; the API checks the permission.
  can_view_usage_dashboard?: boolean
  onboarding_complete: boolean
  // Absent on older servers; only ever a workspace the user is still a member of.
  last_workspace_id?: string | null
  // Set when Open Chat Studio refused the chatbot list at sign-in; absent on older servers.
  ocs_access_denied?: { team: { slug: string; name: string } | null } | null
  agent_model?: { id: string; label: string }
}

export interface AuthSlice {
  user: User | null
  authStatus: AuthStatus
  authError: string | null
  authActions: {
    fetchMe: () => Promise<void>
    login: (email: string, password: string) => Promise<void>
    logout: () => Promise<void>
    dismissOcsAccessNotice: () => Promise<void>
  }
}

export const createAuthSlice: StateCreator<AuthSlice, [], [], AuthSlice> = (set, get) => {
  let requestId = 0
  let pendingMutations = 0
  let mutationQueue = Promise.resolve()

  function mutateSession(operation: () => Promise<void>) {
    pendingMutations += 1
    const result = mutationQueue.then(operation)
    mutationQueue = result.catch(() => undefined)
    return result.finally(() => { pendingMutations -= 1 })
  }

  return {
    user: null,
    authStatus: "idle",
    authError: null,
    authActions: {
      fetchMe: async () => {
        // A visibility refresh must not rediscover A while logout is pending,
        // or supersede an explicit login that has not established its cookie yet.
        if (pendingMutations > 0) return
        const request = ++requestId
        set({ authStatus: "loading", authError: null })
        try {
          // GET sets the CSRF cookie as a side effect
          await api.get("/api/auth/csrf/")
          if (request !== requestId) return
          const user = await api.get<User>("/api/auth/me/")
          if (request !== requestId) return
          set({ user, authStatus: "authenticated" })
        } catch (e) {
          if (request !== requestId) return
          if (e instanceof ApiError && e.status === 401) {
            set({ user: null, authStatus: "unauthenticated" })
          } else {
            set({ user: null, authStatus: "unauthenticated", authError: "Failed to check auth" })
          }
        }
      },

      login: async (email: string, password: string) => {
        const request = ++requestId
        set({ user: null, authStatus: "loading", authError: null })
        try {
          await mutateSession(async () => {
            if (request !== requestId) return
            // Wait for older cookie mutations before refreshing the CSRF token.
            await api.get("/api/auth/csrf/")
            if (request !== requestId) return
            const user = await api.post<User>("/api/auth/login/", { email, password })
            if (request !== requestId) return
            set({ user, authStatus: "authenticated", authError: null })
          })
        } catch (e) {
          if (request !== requestId) return
          const message = e instanceof ApiError ? e.message : "Login failed"
          set({ authStatus: "unauthenticated", authError: message })
          throw e
        }
      },

      logout: async () => {
        ++requestId
        clearUserTenantsCache()
        set({ user: null, authStatus: "unauthenticated", authError: null })
        // Serialize cookie writes so an older logout cannot erase B's new login.
        await mutateSession(() => api.post<void>("/api/auth/logout/"))
      },

      dismissOcsAccessNotice: async () => {
        const user = get().user
        if (user) set({ user: { ...user, ocs_access_denied: null } })
        try {
          await api.post<void>("/api/auth/ocs/access-notice/dismiss/")
        } catch (e) {
          // The next /me brings the notice back, so the user can dismiss it again.
          console.error("Failed to dismiss the Open Chat Studio notice", e)
        }
      },
    },
  }
}
