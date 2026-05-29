# Investigation: Chat home/empty-state differs between deployed and local dev

## Summary

The deployed version of Scout shows a rich chat home screen — the heading
"I'm Scout! Your AI-powered Data Analyst. How can I assist you today?" plus three
provider-specific starter-question cards. The user's local dev shows only a bare
line, "Ask a question about your data to get started.", with no heading and no
cards.

**Root cause: the local dev checkout is running code from *before* commit
`a6f768f`.** The bare empty state and the rich `ChatEmptyState` are two different
implementations of the same screen; the rich one replaced the bare one in that
commit. This is **not a bug in the current code** — it is a stale local checkout
(local `HEAD` is behind `origin/main`).

## Evidence

### Which component renders each variant

- **Rich empty state (deployed / current `main`)** is rendered by
  `frontend/src/components/ChatPanel/ChatPanel.tsx:287-296`:

  ```tsx
  if (messages.length === 0) {
    return (
      <ChatEmptyState
        input={input}
        setInput={setInput}
        onSend={(text) => sendMessage({ text })}
        disabled={isStreaming}
      />
    )
  }
  ```

  The heading and starter cards live in
  `frontend/src/components/ChatEmptyState/ChatEmptyState.tsx`:
  - heading "I'm Scout! ... How can I assist you today?" at lines 96-100
  - starter-question cards mapped from `getStarterQuestions(provider)` at lines
    104-119
  - starter content (including "How many workers completed verified visits this
    week?") in `frontend/src/components/ChatEmptyState/starterQuestions.ts`
    (the `commcare_connect` provider list, line 12).

- **Bare empty state (local dev)** was rendered *inline* inside `ChatPanel.tsx`,
  in the pre-`a6f768f` version of the file. Recovered from git
  (`git show a6f768f^:frontend/src/components/ChatPanel/ChatPanel.tsx`):

  ```tsx
  {messages.length === 0 && (
    <div className="text-center text-muted-foreground mt-20">
      Ask a question about your data to get started.
    </div>
  )}
  ```

  This string does **not** exist anywhere in the current working tree — confirmed
  by grepping `frontend/src`. It only exists in git history at and before
  `a6f768f^`.

### The condition that selects between them

In both old and new code the selector is identical: `messages.length === 0`
(an empty thread). There is **no feature flag, env var, or build-time switch**
involved. The only difference is *what gets rendered* for that empty case:

- Old code: inline `<div>Ask a question about your data to get started.</div>`
- New code: `<ChatEmptyState />` (heading + provider-specific starter cards +
  prominent input)

So the divergence is purely a matter of which revision of `ChatPanel.tsx` is
checked out, not runtime conditions.

### Git history

```
a6f768f  Render ChatEmptyState for empty threads; move Share to TopBar   (2026-05-28)
5a80f9e  Add ChatEmptyState component
fafc425  Add provider-specific starter questions
```

- `git log --all -S "Ask a question about your data"` returns exactly one commit,
  `a6f768f` — the commit that *removed* the string.
- `git merge-base --is-ancestor a6f768f main` → in `main`.
- `git merge-base --is-ancestor a6f768f origin/main` → in `origin/main`.

Because the rich `ChatEmptyState` is present in both `main` and `origin/main`,
the deployed build (built from `main`) shows the rich screen. A local dev
environment showing the bare string must be on a branch/commit that predates
`a6f768f` (e.g. the developer hasn't pulled, or is on an older feature branch).

## Conclusion

- **Not a bug.** Current `main` already renders the rich empty state for every
  empty thread; the gating condition (`messages.length === 0`) is the same in
  both versions and is correct.
- **Action for the affected developer:** update the local checkout
  (`git pull` / rebase onto `main`) and rebuild the frontend
  (`cd frontend && bun run build` or restart `bun dev`). The bare string will
  disappear and the rich `ChatEmptyState` will render.
- No code change is required to "fix" Part A. (Part B — wiring slash commands
  into the empty-state input — is addressed separately.)
