# OCR review gate implementation plan

**Goal:** Run OCR with Anthropic Opus 5 on every PR and authorized `@ocr` comment, then run Claude review only after a complete review without high/critical findings.

**Architecture:** A trusted `pull_request_target` / `issue_comment` workflow resolves the current PR and authorizes manual callers using live repository permissions. OCR reads git objects from a trusted checkout. A JSON gate validates coverage, reviewed commit, and severity. Claude follows only for same-repository PRs. Existing manual `@claude` behavior remains in its own workflow.

**Tech stack:** GitHub Actions, pinned Alibaba OCR v1.12.2, Node built-in test runner, existing Anthropic secret.

## Tasks

- [x] Add `.github/scripts/ocr-gate.cjs` and behavior tests: permit complete clean/low/medium reports, block high/critical or unknown severity, incomplete/malformed reports, incorrect SHAs, and failed comment publication.
- [x] Add `.github/workflows/ocr.yml`: automatic and manual triggers, live write-permission check, cancellation after authorization, trusted checkout, pinned action/CLI, explicit Anthropic model/thinking, bounded concurrency/budget, gate summary, and same-repository Claude follow-up.
- [x] Remove the independent automatic review from `.github/workflows/claude.yml`; preserve manual behavior.
- [x] Add gate tests to CI and document usage, significant severity threshold, fork behavior, costs and limitations.
- [x] Validate Node tests and actionlint; independently review workflow security and spec compliance.
- [ ] Publish and activate the configuration using GitHub admin access, then exercise a manual run on a suitable PR and inspect results. Do not claim a live model review passed until it actually does.

## Validation cases

A normal unrelated PR comment must not cancel a running review. Unauthorized `@ocr` comments must not spend API quota. A moved PR head must not receive a stale Claude follow-up. An OCR budget stop or unclassified finding must not pass. Fork reviews must never execute head-controlled code with secrets. All feedback must remain visible even when the significant-finding gate blocks Claude.
