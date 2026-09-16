# Incremental automated review

## Approved intent

Review new changes after a completed review, preserve blockers, avoid repeated whole-PR context, and retain an explicit full-review escape hatch. This implements the approach approved in the conversation with “Make it so”.

## Design

Use the pinned OCR action's native checkpoint ranges. Scout records a versioned gate state in its existing bot-owned sticky comment. Only a complete, published, non-blocking review is an eligible baseline. A later run may narrow only when the action's checkpoint head and source run exactly match that accepted state, the captured base and Scout policy fingerprint match, and Git proves ancestry. Missing, ambiguous, malformed or mismatched state fails closed. A preflight compares the native checkpoint with Scout’s accepted head/run before spending on an incremental review, avoiding a wasted delta after cancellation between checkpoint and gate publication. A failed/blocked previous gate forces a full review, which re-examines earlier findings rather than letting a clean unrelated delta erase them. Native OCR independently invalidates checkpoints for changed rules/configuration and rewritten ancestry.

Manual `@ocr full` forces a full review; `budget=N` can be combined with it. The completeness gate validates the actual range rather than always expecting the merge-base. Existing severity and coverage checks stay in place. A same-head explicit rerun uses a full review.

Claude follows the selected delta only when its own previous action completed successfully for the accepted checkpoint. Otherwise it reviews the full PR. Its prompt also asks to revisit earlier unresolved findings. Record successful Claude completion separately; OCR success never implies Claude completion. Use a focused prompt instead of invoking a plugin whose full-PR algorithm can override the range.

Use native small-change bundling, and route low-severity findings to the sticky summary. The pinned action exposes no grouping threshold or fallback override. Do not vendor/fork its review engine just to change grouping: document that large-set malformed grouping still falls back per file, and report it in the gate summary. Range reduction is the principal token saving. No budget increase.

## Validation

Unit-test state trust, clean checkpoint selection, blocked/partial transitions, policy/base changes, source-run/range mismatch, force-push, and explicit full commands. Test the actual workflow scripts with mocked GitHub responses and Git ancestry. Run existing completeness tests and actionlint. Independently review the implementation. Publish a dedicated infrastructure PR and verify its checks before integrating.
