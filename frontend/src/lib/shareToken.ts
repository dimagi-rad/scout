import { BASE_PATH, stripBase, withBasePath } from "@/config"

export type ShareKind = "threads" | "runs" | "recipes"

/**
 * Extract a share token from a `/shared/{kind}/{token}` pathname.
 *
 * The base path (e.g. "/scout" on the labs deployment) is stripped first so the
 * anchored regex matches regardless of the mount point. Returns undefined when
 * the path is not a share URL of the given kind (issue #248, 04#8c).
 *
 * @param pathname  Raw, unstripped pathname (e.g. window.location.pathname).
 * @param kind      "threads" or "runs".
 * @param base      Base prefix to strip; defaults to the configured BASE_PATH.
 */
export function parseShareToken(
  pathname: string,
  kind: ShareKind,
  base: string = BASE_PATH,
): string | undefined {
  const stripped = stripBase(base, pathname)
  const match = stripped.match(new RegExp(`^/shared/${kind}/([^/]+)`))
  return match?.[1]
}

const SHARE_API_PATHS: Record<ShareKind, (token: string) => string> = {
  threads: (token) => `/api/chat/threads/shared/${token}/`,
  runs: (token) => `/api/recipes/runs/shared/${token}/`,
  recipes: (token) => `/api/recipes/shared/${token}/`,
}

/**
 * Build the API URL for a shared thread/run/recipe, prefixed with the
 * configured base path so it resolves under the deploy mount point.
 */
export function shareApiUrl(kind: ShareKind, token: string): string {
  return withBasePath(SHARE_API_PATHS[kind](token))
}
