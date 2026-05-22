import { createContext, useContext, type ReactNode } from "react"
import {
  useWorkspaceJobsImpl,
  type UseWorkspaceJobs,
} from "@/hooks/useWorkspaceJobs"

const WorkspaceJobsContext = createContext<UseWorkspaceJobs | null>(null)

interface ProviderProps {
  workspaceId: string | null
  children: ReactNode
}

/**
 * Single owner of the workspace-jobs polling loop. Mount once at the layout
 * level (above any component that needs job state) so all consumers share the
 * same `setInterval` and `prevThreadIdsRef`. Multiple providers in the tree
 * would re-introduce the duplicate-polling bug we are fixing.
 */
export function WorkspaceJobsProvider({ workspaceId, children }: ProviderProps) {
  const value = useWorkspaceJobsImpl(workspaceId)
  return (
    <WorkspaceJobsContext.Provider value={value}>
      {children}
    </WorkspaceJobsContext.Provider>
  )
}

// eslint-disable-next-line react-refresh/only-export-components
export function useWorkspaceJobs(): UseWorkspaceJobs {
  const ctx = useContext(WorkspaceJobsContext)
  if (ctx === null) {
    throw new Error(
      "useWorkspaceJobs must be used within a WorkspaceJobsProvider",
    )
  }
  return ctx
}
