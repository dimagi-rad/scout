# Error contract: finishing the adoption

**Status:** proposal, seeking alignment
**Date:** 2026-08-11
**Context:** follow-up to the #388 review; relates to #363, #372, and the `pd-wave:3` cluster

## Summary

Scout already has an error contract — a code registry, a written rule, and ~30 call
sites in the MCP layer. The Django and frontend surfaces never adopted it. This
proposes finishing that adoption, in five reviewable PRs, starting with a refactor
that gives credential remediation copy a single owner.

**This is not a new abstraction.** It is the existing `apps/common/error_codes.py`
convention applied to the surfaces that skipped it.

## The problem, in two findings

Both were found while verifying #388, and neither has an issue yet.

**1. The advice contradicts itself across surfaces.** For an upstream access
removal, the chat failure card says:

> reconnecting will NOT restore it, because it mints a token with exactly the same access

while `apps/workspaces/access.py:66-67` says, for the same condition:

> Access may have been removed upstream — **reconnect** or ask an admin.

and `LostAccessModal.tsx:101-103`, a full-screen gate, says:

> **Reconnect your account** or ask an admin to restore access.

This is #372 — telling a user to reconnect when reconnecting provably cannot help —
reproduced on two surfaces #388 does not touch. `access.py` owns a separate
vocabulary (`not_member` / `tenant_access_lost`) unrelated to `ErrorCode`, and its
own prose.

**2. The most common credential failure produces no advice at all.** A pre-flight
`CredentialResolutionError` (`tasks.py:385-398`) computes both a message and a code,
appends them to `tenant_results`, and `continue`s. `MaterializationRun` is created
*inside* `run_pipeline` (`materializer.py:196`), which is never reached — so no run
row exists, `_build_failure_detail_for_job` returns empty, and the user gets
`"Data loading failed before it could complete. Please retry your request."`

All the code-keyed machinery applies only to tokens that die *mid-run*.

**Scale:** 23 distinct places can tell a user what to do about a credential or access
problem. Exactly one is gated on an `ErrorCode`.

## What we already have

`apps/common/error_codes.py` states the rule:

> A code is the **machine-readable** half of an error. It is stable, never localised,
> and never reworded — handlers, UI, and prompts branch on it. The message beside it
> is the **human-readable** half: free to change, and never parsed. Every error that
> crosses a boundary carries both. Neither is derived from the other.

`mcp_server/envelope.py:77` implements it as `{"success", "error": {"code", "message",
"detail"}}`, with ~30 call sites in `mcp_server/server.py`. The registry records why:
substring-matching `'"code": "NOT_FOUND"'` in tool output broke the moment FastMCP
changed its JSON separators (finding 06#1).

The gap is that Django views, task summaries, and the frontend never picked it up.

## Precedent in Dimagi codebases

Checked `commcare-hq`, `open-chat-studio`, and `commcare-connect` before proposing.

**commcare-hq — the mechanism, running in production for years.**
`MessagingEvent.ERROR_MESSAGES` (`corehq/apps/sms/models.py`) is a code → prose dict.
The code is load-bearing, not decorative: three independent surfaces read the one
dict — the HTML report (`reports/standard/message_event_display.py:68`), the public
REST API (`api/resources/messaging_event/serializers.py:45-46`, returning
`{"code": event.error_code, "message": ERROR_MESSAGES.get(...)}`), and the dashboard
chart (`messaging/scheduling/views.py:257`, which merges codes to consolidate
opt-out errors). The code is also a documented API filter.
*Caveat:* it is one team's local convention — no enum type, no exception base class,
no documentation.

**open-chat-studio — the rule, and the cautionary tale.** No code registry. But
ADR-0033 ("Structured runtime Jinja error messages") already argues our position:
*format every exception through a shared helper that categorises it and produces an
actionable message.* ADR-0010 states the sole-owner rule in the language we'd want.
ADR-0039 ships one code-as-contract boundary — 403s carry `session_token_required` /
`session_token_invalid` / `session_expired`, and the widget discards the server prose
and owns the wording via i18n keys.

OCS also demonstrates the failure mode precisely: one `UserReportableError` yields raw
exception text on web, an **LLM paraphrase** on Telegram (`channels.py:926`), and
silence on the web channel — plus three generic fallback strings, two byte-identical.

**commcare-connect — precedent for the wire format only.**
`commcare_connect/utils/error_codes.py` is a six-member `StrEnum` emitted as a bare
`{"error_code": ...}` with no prose at all. Clean, but the consumer is the Android app
and the contract is undocumented; nothing in-repo reads the codes.

**Read:** the pattern is established at Dimagi. HQ proves the mechanism, OCS argues the
rule and shows the cost of skipping it. Neither is a formal standard, so this is
"consistent with how we already work," not "conforming to a house style."

## Proposal

Three PRs. **PR2 and PR3 are independent of each other** — different files, either
order, or concurrently. Nothing stacks.

| PR | Story |
|---|---|
| **1** | Loaders describe, presentation advises — #388 + `71fb111` |
| **2** | The failure summary tells the truth |
| **3** | One owner for credential advice |

### PR1 — loaders describe, presentation advises

Already open and CI-green. Fold in `71fb111` (all four loader raise sites become
descriptions; the five reporting rules land in `apps/common/errors.py`) and narrow the
body. This is the direct answer to the review comment.

### PR2 — the failure summary tells the truth

| # | Commit |
|---|---|
| 1 | `fix(materializer): stop truncating a source failure mid-sentence` |
| 2 | `fix(tasks): render every failed source, not just the first` |
| 3 | `fix(tasks): stop overwriting a real summary with generic retry advice` |
| 4 | `feat(tasks): record a pre-flight credential failure as a run result` |

"Make the pipe faithful, then send more through it." Commits 1-3 are small and
independently revertable; commit 4 is the structural one and the place to look.

### PR3 — one owner for credential advice

| # | Commit | Behaviour change |
|---|---|---|
| 1 | `refactor(errors): give credential remediation copy a single owner` | no — pure move |
| 2 | `fix(access): stop telling users to reconnect when access was removed upstream` | **yes** |
| 3 | `fix(frontend): key lost-access advice off the error code` | **yes** |
| 4 | `refactor(credential_resolver): describe the failure, don't advise` | no |
| 5 | `feat(chat): offer Reconnect only when the credential is actually dead` | **yes** |
| 6 | `test: fail the build on advice at a raise site` | guard |

Commit 1 is a no-op refactor, so commits 2-5 read as small deletions plus a call.

### Deliberately not in this sequence

`a9bcc1f` (retire the `[cascade-teardown]` sentinel) and **#363** (re-run advice
branching on reason). Different subsystem, already tracked, and already `pd-wave:1` —
which outranks this cluster. It should not queue behind three PRs of error-contract
work.

## Decisions wanted

1. **Scope of the owner module:** credential/access codes only, or all user-facing
   remediation copy? Starting narrow is safer; starting broad avoids a second pass.
2. **Is pulling #363 out right**, or would you rather it ride along?
3. PR2 commit 4 may prove big enough to want its own PR. Comfortable with that call
   being made when it is written, rather than now?

## Non-goals

- Localisation. Codes make it possible later; nothing here depends on it.
- Rewriting the ~80 DRF `{"error": "..."}` bodies. Out of scope until there is a
  consumer that needs them coded.
- The `[__system_resume__]` sentinel (`apps/chat/constants.py:5`) — same anti-pattern,
  different subsystem, worth its own issue.

## Known-but-unfiled

Filing these so they don't evaporate the way the message-coherence half of the #388
review did: pre-flight credential failure produces no guidance; `access.py` /
`LostAccessModal` contradict the card; `MATERIALIZATION_FAILED_MESSAGE` overwrites a
real summary; 200-char truncation (OCS 403 truncates every time — the message is 215
chars with a UUID); only the first failed source's message renders;
`get_materialization_status` returns `success=True` for a fully failed run;
`SCHEMA_BUILD_FAILED` is unwritable at runtime.
