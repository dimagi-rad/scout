import { AlertTriangle } from "lucide-react"
import { CONNECT_QUICK_CHECK_LIMIT } from "@/lib/connectAccessCheck"
import { cn } from "@/lib/utils"

interface Props {
  /** Connect opportunities the workspace has, or would have. */
  count: number
  /** Opens the notice, e.g. "This workspace has". */
  lead: string
  testId: string
  className?: string
  /** Whether the viewer can act on "split it": managers, or someone creating one. */
  canSplit: boolean
}

/** Warns, never blocks: a large Connect workspace works, but its access check is slow. */
export function ConnectSizeNotice({ count, lead, testId, className, canSplit }: Props) {
  if (count <= CONNECT_QUICK_CHECK_LIMIT) return null
  return (
    <div
      className={cn(
        "flex gap-2 rounded-md border border-amber-500/30 bg-amber-500/10 p-3 text-xs text-amber-700 dark:text-amber-400",
        className,
      )}
      data-testid={testId}
    >
      <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden />
      <p>
        {lead} {count} CommCare Connect opportunities. Scout checks your access one
        opportunity at a time for up to {CONNECT_QUICK_CHECK_LIMIT}; above that it reads your
        whole Connect opportunity list instead. For people with many Connect opportunities that
        can time out, so opening the workspace may keep asking you to retry verifying access.
        {canSplit && ` Workspaces of ${CONNECT_QUICK_CHECK_LIMIT} or fewer avoid this.`}
      </p>
    </div>
  )
}
