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
}

/** Warns, never blocks: a large Connect workspace works, but its access check is slow. */
export function ConnectSizeNotice({ count, lead, testId, className }: Props) {
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
        {lead} {count} CommCare Connect opportunities. Scout checks your access quickly for up to {CONNECT_QUICK_CHECK_LIMIT};
        above that it reads your whole Connect opportunity list, which can be slow. Opening the
        workspace may take longer or ask you to retry verifying access. Splitting it into
        workspaces of {CONNECT_QUICK_CHECK_LIMIT} or fewer avoids this.
      </p>
    </div>
  )
}
