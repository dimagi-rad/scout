export interface AccountSessionScope {
  // Bound to one login generation, not just a user ID (A → logout → A is new).
  // Capture this before awaiting work that has non-store side effects.
  accountSession: { isCurrent: () => boolean }
}
