# Incremental Review Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development for the independent state helper; coordinate workflow wiring in the parent task.

**Goal:** Reduce repeated review work without allowing earlier blocking findings or incomplete review coverage to disappear.

**Architecture:** Native OCR checkpoints plus a Scout-owned accepted-gate state, exact range validation, and a separate Claude completion marker. Trusted base scripts run with credentials; PR contents are inspected as Git objects only.

**Tech Stack:** GitHub Actions, Node CommonJS, node:test, pinned OCR 1.12.2.

## Task 1: Review state and command parsing

- [x] Add `.github/scripts/ocr-state.cjs` and behavior tests: trusted bot state; version/schema validation; policy/base/ancestry safeguards; run-ID equality; explicit full and budget command parsing.
- [x] Preserve existing `ocr-gate.cjs` severity and coverage validation; pass the verified selected range into it.

## Task 2: Workflow integration

- [x] Checkout captured trusted base and snapshot policy helpers before upstream checkout.
- [x] Load prior gate state and force full if it cannot authorize a checkpoint.
- [x] Enable native checkpoints; supply full override; validate outputs against prior accepted state before the existing gate.
- [x] Record gate result and reviewed range; preserve separate Claude state and report grouping fallback.
- [x] Run focused Claude review for a verified delta only after a matching successful prior Claude run; otherwise use the full range. Persist its successful completion separately.

## Task 3: Verification and delivery

- [x] Add workflow integration tests, update CI test glob, document range resets, full commands, and grouping limits.
- [x] Run Node tests and actionlint, then independent review.
- [ ] Create dedicated PR, validate CI/review, integrate and confirm deployed workflow behavior.
