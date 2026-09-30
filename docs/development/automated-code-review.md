# Automated PR review

Scout runs **Open Code Review (OCR)** first on PR creation, updates and reopening, including fork PRs. It uses the existing `ANTHROPIC_API_KEY` Actions secret and Anthropic `claude-opus-5`.

OCR posts actionable inline findings, routes low-severity findings to its sticky summary, and maintains review checkpoints. A separate sticky **OCR gate** comment explains whether the Claude follow-up can run:

- **High or critical findings:** block Claude; address the findings and push again.
- **Low or medium findings only:** allow Claude to run.
- **Failed, partial, budget-limited, waived-file, malformed or unclassified results:** block Claude until a complete review can establish the outcome.
- **No OCR-reviewable files** (OCR filters out Markdown, other non-code paths and Scout's excluded test files, so a docs-only or tests-only PR selects nothing): OCR reports a skipped run with empty coverage. The gate passes it, and Claude reviews its usual range (the whole PR unless it completed the accepted checkpoint), told that no OCR review covers the skipped range. A skipped run with any finding, coverage entry, failure or mismatched range still blocks. On fork PRs, which get no Claude follow-up, it blocks instead of reporting a review that never happened.
- **Fork PRs:** receive OCR feedback; automatic Claude follow-up is disabled.

The first review covers the full PR. After a complete review passes the gate, the next push can review only changes since that accepted head. The gate validates both ends of that range, the originating workflow run and Git ancestry. Duplicate-comment suppression is separate from incremental review: it reduces noise, not the amount of code reviewed.

A blocked or incomplete review forces the next run back to a full review. OCR itself can save checkpoints even when it finds blockers; Scout deliberately does not trust those as clean baselines. This prevents an unrelated clean delta from silently clearing earlier high/critical findings. Missing or malformed state, a moved merge-base (merging or rebasing onto the base branch), changed review policy, rewritten history and explicit full requests also cause a full review. The base branch advancing on its own does not: GitHub updates the PR's base commit whenever the base branch moves, so Scout compares the merge-bases of the accepted and current heads (through the compare API) and keeps the checkpoint when they match. If that lookup fails, the review is full. A same-head rerun is treated as an explicit re-review.

The accepted state is stored in the bot-owned sticky gate comment. The gate considers all findings in the selected range, including findings whose duplicate inline comments were suppressed. If the PR changes during review, rerun against the new commits. Files OCR excludes before selection remain outside its coverage guarantee.

Claude uses the same delta only if it also completed a non-blocking review of the accepted checkpoint. Otherwise Claude reviews the full PR. Its focused prompt additionally revisits prior unresolved findings. A failed, partial or budget-limited Claude run never advances that separate checkpoint.

The separate Claude receipt starts as **pending** and changes to **verified** or **blocked**. Verification requires a successful action and final SDK result, no reported tool permission denials, complete structured output for the exact head, and a new trusted bot comment bearing the run's unique receipt. The receipt binds repository, PR, run attempt, head and base. Its nonce is masked and handed to the reviewer through a private runner file, never an echoed action input or output. It becomes public only with the review artifact; it prevents accidental or pre-artifact replay, but is not a boundary against the reviewer or trusted code in that job. High/critical findings fail the review check and block checkpoint advancement even when the review itself completed. Missing or malformed evidence also blocks. Failed attempts update the visible receipt; older attempts cannot replace a newer receipt. Diagnostics omit raw transcripts, tool arguments and exception content. A verified receipt and a reread confirming successful checkpoint publication are required before a future delta can reuse the Claude baseline.

The gate controls the Claude follow-up. It does not add a required branch-protection check or replace human review.

## Manual runs

Post a new PR conversation comment using one of these commands:

- `@ocr`: use an eligible checkpoint, otherwise review the full PR.
- `@ocr full`: deliberately review the entire PR again.
- `@ocr budget=1000000`: override the token budget for this run.
- `@ocr full budget=1000000`: combine a full review and a one-run budget.

`full` and `budget=N` may appear in either order. Commands are case-insensitive. Unknown or repeated options are rejected with a notice before starting or cancelling a review. Each command runs the same pipeline, including the eligible Claude follow-up.

Only users whose current repository permission is write, maintain or admin can trigger manual OCR runs. Bot comments, unrelated comments, closed PRs and unauthorized commands do not start a review or cancel an active one. Editing an existing comment does not trigger a run.

Manual **`@claude`** requests retain their existing workflow and operate independently of the OCR gate.

## Trust and credentials

The OCR workflow uses `pull_request_target` so fork PRs can be reviewed with the repository secret. The pinned upstream action checks out the trusted base branch, fetches PR Git objects and reads the diff without checking out or executing fork code. Scout loads and snapshots the gate scripts from the immutable `github.workflow_sha` revision before the upstream checkout (an older PR comparison base can predate those scripts) and fingerprints the workflow and validation policy. Prior review text is fetched through fixed read-only SDK methods and supplied as untrusted JSON data; Claude does not receive a general `gh api` shell grant. The automatic Claude step also keeps the trusted checkout and uses Git/PR reads to inspect the requested commits; it runs only on same-repository PRs.

Do not change this workflow to check out a fork head or execute its install/build/test scripts while credentials are available. Review text is still untrusted input to the models.

GitHub attaches checks from `@ocr` (`issue_comment`) runs to the default branch, not the PR. Without extra work the PR would keep showing the `review` check from the last `pull_request_target` run even after a re-run passed (PR #501). A separate `review-check` job therefore runs after `review` on authorized `@ocr` runs. It creates one completed check run named `review` on the PR head, with the re-run's success or failure. Cancelled runs record nothing, because the run that superseded them records its own result. Only that job has `checks: write`, so the OCR action and the Claude step never receive it. Nothing marks `review` as a required check. On `pull_request_target` runs the job shows as skipped.

Manual command authorization happens before the per-PR cancellation group. New authorized reviews cancel older runs for the same PR.

## Model, cost and version settings

Configuration lives in `.github/workflows/ocr.yml`:

- Self-updates disabled with job-level `OCR_NO_UPDATE=1`, including the initial version check. Without this, the npm launcher can replace the pinned install while later workflow steps are using it.
- OCR action pinned to commit `b3dbcb634cbb39344e0a3c48ccb1cef3ecd51532`, CLI `1.12.2`.
- Anthropic Opus 5, adaptive thinking, high model effort; low OCR review effort (one review round per file group). OCR's medium effort runs a second full agent loop for every group whose first round found anything. Across ~270 runs that second round ran for about 240 groups and cost about a quarter of all OCR tokens. It added findings in about 100 groups. The logs do not say which round produced a finding, but three quarters of all OCR findings are low severity, and the Claude follow-up reviews the same diff afterwards.
- Four concurrent OCR tasks, 15-minute per-task timeout, and a total-token budget scaled to PR size (500,000 to 4,000,000; see [Token budget and manual overrides](#token-budget-and-manual-overrides)).
- `.github/ocr-rule.json` excludes tests (`tests/` directories, `test_*.py`, `conftest.py`, `*.test.cjs`) and `package-lock.json` from OCR. OCR's built-in test excludes only match `*_test.py`, so Scout's pytest files were reviewed and made up about 40% of selected files. The Claude follow-up still reviews tests. The rule file is snapshotted from the trusted workflow revision and is part of the policy fingerprint.
- Native cross-push checkpoints enabled, subject to Scout’s accepted-state validation.
- Low-severity findings go to the summary; their severity is still evaluated by the gate. The review prompt asks for demonstrated defects rather than speculative API mismatches or style/test-coverage requests without a concrete failure.
- 45-minute job timeout, including the Claude follow-up.
- Claude follow-up uses Opus 5 with a $10 CLI budget.

The OCR token limit stops further dispatch after it is exceeded; in-flight work can overshoot. It is not a hard dollar spending limit. Configure an Anthropic workspace spending limit for a billing ceiling. Both review stages consume API quota; low/medium-only PRs still receive both reviews. The limit gates whether each file group may start rather than capping spend; see [How the token budget works](#how-the-token-budget-works-size-prs-by-file-groups-not-lines).

The release and provider settings are explicit to make upgrades reviewable. When upgrading OCR, verify its output contract, re-check where `dispatchSubtasks` applies the token budget (see [How the token budget works](#how-the-token-budget-works-size-prs-by-file-groups-not-lines)) and update that section if it moved, and run:

```sh
node --test .github/scripts/ocr-*.test.cjs .github/scripts/claude-review-*.test.cjs
actionlint .github/workflows/ocr.yml .github/workflows/claude.yml .github/workflows/ci.yml
```

Inspect the Actions log, OCR JSON artifact and sticky gate comment when a run blocks. OCR action success alone does not mean a clean or complete review.

## Upstream references

- [OCR CI integration](https://github.com/alibaba/open-code-review/blob/v1.12.2/pages/src/content/docs/en/integrations/ci.md)
- [Pinned action inputs and outputs](https://github.com/alibaba/open-code-review/blob/v1.12.2/action.yml)
- [Anthropic Opus 5](https://platform.claude.com/docs/en/models/opus-5/whats-new-opus-5)

### Token budget and manual overrides

An authorized collaborator can request a one-run token budget with `@ocr budget=5000000` (maximum 5 million). Without `budget=`, automatic runs and plain `@ocr` scale the budget with the PR's changed files and lines (from the PR API, since OCR only prints its own estimate after dispatch):

| PR size | Budget |
|---|---|
| under 10 files and under 500 lines | 500,000 |
| under 25 files and under 2,000 lines | 2,000,000 |
| larger | 4,000,000 |

A repeat full review (policy change, base change or a blocked prior run) is at least 2,000,000. A PR's first review (no earlier gate state), including `@ocr full`, uses the size tier alone. An explicit `budget=N` always wins. Real usage over ~260 runs had a median of about 306K tokens, a mean of about 644K and a maximum of 6.34M, so a flat 500K exhausted most medium and large PRs and forced a manual re-run.

Cost trade-off: a higher ceiling avoids a wasted run per push but lets a run spend more. The budget gates whether a file group may start, so small groups do not consume it; at Opus rates a million tokens cost roughly $1.9. The review job's wall-clock limit is 45 minutes; the longest executed review in about 300 runs took 31 (OCR 20, Claude 11). A timeout leaves no gate comment, so check the run log first if a review is cancelled. This is a soft limit: in-flight work can overshoot it. A larger budget does not relax finding severity or completeness checks.

## Grouping and remaining limits

The pinned CLI already bundles small change sets without an LLM grouping call. Narrowing follow-up reviews makes that path more useful and reduces repeated context. Larger change sets use its semantic grouping model. If that model returns malformed JSON, OCR falls back to per-file tasks; Scout reports that fallback in the gate summary because it can inflate token usage.

Version 1.12.2 exposes no action input or CLI configuration for changing that fallback or grouping thresholds. Fixing the large-review fallback requires an upstream change, rather than an undocumented local override. Checkpointing does not resume a partially completed first review: a complete accepted baseline is required. Cached input is included in the reported token budget, so high token counts do not all represent newly generated text or full-price input.

### How the token budget works (size PRs by file groups, not lines)

OCR splits a PR into **file groups** of up to 10 related files (tiny change sets become one group, and an oversized group is split per file). Each group gets its own agentic review. The budget only decides whether a group may *start*. In OCR 1.12.2 (`dispatchSubtasks` in `internal/agent/agent.go`), each group is checked before it waits for one of the `review_concurrency` slots, and the check compares tokens used so far plus an estimate of that group's **diff size**. The estimate does not include the review loop, which costs far more. As a result:

- The first `review_concurrency + 1` groups (**5** with our setting of 4) pass the check before any review spend is recorded, so **they always run in full, however many tokens they burn**.
- Group 6 and later are checked only once a slot frees up, against real usage. If the budget is already spent they are skipped (`token budget reached … skipping group` in the log). Those files count as uncovered, so the gate blocks the review as incomplete.

So a PR that OCR splits into five or fewer groups passes the budget regardless of size. A PR with many unrelated areas can hit the limit even when every file is small. Keep PRs to a few cohesive areas. If a legitimately wide PR is blocked on budget, split it or re-run with `@ocr budget=N`. There is no action input to cap the group count or move the check after the slot is acquired. Groups run one at a time per slot and each takes several minutes of sequential model turns, so wall time scales with groups divided by `review_concurrency`. It is 4 (the CLI default is 8); lowering it only shrinks the always-run count and slows every review.
