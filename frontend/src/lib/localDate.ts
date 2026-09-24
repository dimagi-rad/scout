// Formats the local calendar day. toISOString() reports the UTC day instead, which is
// the previous day in every zone ahead of UTC until UTC midnight catches up.
export function localIsoDate(date: Date): string {
  const month = String(date.getMonth() + 1).padStart(2, "0")
  const day = String(date.getDate()).padStart(2, "0")
  return `${String(date.getFullYear()).padStart(4, "0")}-${month}-${day}`
}
