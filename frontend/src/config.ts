/**
 * Runtime base path for the app, derived from the Vite build-time env var.
 * Defaults to "" (root) for local development.
 *
 * Examples:
 *   local dev:    ""
 *   connect-labs: "/scout"
 */
export const BASE_PATH = (import.meta.env.VITE_BASE_PATH || "").replace(/\/$/, "")

/**
 * Join an app-absolute path (e.g. "/health/", "/api/...") onto a base prefix.
 *
 * Pure helper so it can be unit-tested with an explicit base. Prefer
 * {@link withBasePath} in app code, which closes over the configured
 * {@link BASE_PATH}.
 */
export function joinBase(base: string, path: string): string {
  const normalizedPath = path.startsWith("/") ? path : `/${path}`
  if (!base) return normalizedPath
  return `${base}${normalizedPath}`
}

/**
 * Strip a base prefix from a pathname (e.g. "/scout/shared/x" → "/shared/x").
 *
 * Pure helper so it can be unit-tested with an explicit base. Prefer
 * {@link stripBasePath} in app code.
 */
export function stripBase(base: string, pathname: string): string {
  if (!base) return pathname
  if (pathname === base) return "/"
  return pathname.startsWith(`${base}/`) ? pathname.slice(base.length) : pathname
}

/**
 * Prefix an app-absolute URL with the configured {@link BASE_PATH} so it
 * resolves correctly under a deploy mount point (e.g. /scout) where the proxy
 * only forwards requests under that prefix. Root-relative URLs that skip this
 * hit the host root and 404 on the labs deployment (issue #248, 04#8).
 */
export function withBasePath(path: string): string {
  return joinBase(BASE_PATH, path)
}

/**
 * Remove the configured {@link BASE_PATH} from a pathname before matching it
 * against app routes (e.g. share-token regexes anchored at ^/shared/).
 */
export function stripBasePath(pathname: string): string {
  return stripBase(BASE_PATH, pathname)
}
