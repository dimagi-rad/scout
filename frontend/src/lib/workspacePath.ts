/**
 * Human-readable workspace URLs, Notion/Linear style: a cosmetic slug derived
 * from the display name, followed by the workspace UUID. The UUID stays the
 * source of truth — lookups are always by UUID, so renames never break links
 * and old `/workspaces/<uuid>` URLs keep working.
 */

const MAX_SLUG_LENGTH = 60

/**
 * Turn a workspace name into a URL-safe slug:
 * lowercase, every run of non-alphanumeric chars collapsed to a single "-",
 * leading/trailing "-" trimmed, capped to ~60 chars. Falsy/empty → "".
 */
export function slugifyWorkspaceName(name: string): string {
  if (!name) return ""
  return name
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, MAX_SLUG_LENGTH)
    .replace(/-+$/g, "")
}

/**
 * Build the detail/settings path for a workspace: `/workspaces/<slug>/<id>`
 * when a slug is derivable (preferring `display_name`, falling back to `name`),
 * else the bare `/workspaces/<id>`. Returns the path WITHOUT any embed prefix —
 * callers prepend their own `pathPrefix` as they do today.
 */
export function workspacePath(ws: {
  id: string
  display_name?: string
  name?: string
}): string {
  const slug = slugifyWorkspaceName(ws.display_name || ws.name || "")
  return slug ? `/workspaces/${slug}/${ws.id}` : `/workspaces/${ws.id}`
}
