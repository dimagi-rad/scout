// The API client has no timeout of its own; a stalled history load would block
// sending in that chat until the page reloads.
export const HISTORY_LOAD_TIMEOUT_MS = 20_000
