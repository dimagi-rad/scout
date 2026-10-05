import type { LoadTimeEstimate } from "@/api/jobs"

function approximateDuration(seconds: number): string {
  return seconds < 60 ? "under a minute" : `about ${Math.round(seconds / 60)} min`
}

export function formatTimeEstimate(
  estimate: Pick<LoadTimeEstimate, "usual_seconds" | "elapsed_seconds"> | null | undefined,
): string | null {
  if (!estimate || !Number.isFinite(estimate.usual_seconds) || estimate.usual_seconds <= 0) {
    return null
  }
  const usual = approximateDuration(estimate.usual_seconds)
  const elapsed = estimate.elapsed_seconds
  if (elapsed == null || !Number.isFinite(elapsed) || elapsed < 0) {
    return `Usually takes ${usual}`
  }
  if (elapsed >= estimate.usual_seconds) return "Taking longer than usual"
  const remaining = approximateDuration(estimate.usual_seconds - elapsed)
  return `${remaining[0].toUpperCase()}${remaining.slice(1)} left · usually takes ${usual}`
}
