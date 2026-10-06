// The API client has no timeout of its own; a stalled history load would block
// sending in that chat until the page reloads. Long enough to ride out the client's
// busy-503 retries when the shared database is short of connections.
export const HISTORY_LOAD_TIMEOUT_MS = 45_000
