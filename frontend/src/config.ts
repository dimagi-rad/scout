/**
 * Runtime base path for the app, derived from the Vite build-time env var.
 * Defaults to "" (root) for local development.
 *
 * Examples:
 *   local dev:    ""
 *   connect-labs: "/scout"
 */
export const BASE_PATH = (import.meta.env.VITE_BASE_PATH || "").replace(/\/$/, "")
