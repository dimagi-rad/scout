interface Props {
  role: string
}

export function RoleBadge({ role }: Props) {
  const styles: Record<string, string> = {
    manage: "bg-green-950 text-green-400",
    read_write: "bg-blue-950 text-blue-400",
    read: "bg-muted text-muted-foreground",
  }
  const labels: Record<string, string> = {
    manage: "Manager",
    read_write: "Read-Write",
    read: "Read",
  }
  return (
    <span
      className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ${styles[role] ?? styles.read}`}
    >
      {labels[role] ?? role}
    </span>
  )
}
