/** A source's own description under its name in a picker; nothing when it has none. */
export function SourceDescription({
  description,
  testId,
}: {
  description: string | undefined
  testId: string
}) {
  const text = description?.trim()
  if (!text) return null
  return (
    <span
      className="mt-0.5 line-clamp-2 text-xs whitespace-normal text-muted-foreground/90"
      title={text}
      data-testid={testId}
    >
      {text}
    </span>
  )
}
