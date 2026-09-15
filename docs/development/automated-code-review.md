# Automated PR review

Scout runs **Open Code Review (OCR)** first on PR creation, updates and reopening, including fork PRs. It uses the existing `ANTHROPIC_API_KEY` Actions secret and Anthropic `claude-opus-5`.

OCR posts inline findings and maintains a summary. A separate sticky **OCR gate** comment explains whether the Claude follow-up can run:

- **High or critical findings:** block Claude; address the findings and push again.
- **Low or medium findings only:** allow Claude to run.
- **Failed, partial, budget-limited, waived-file, malformed or unclassified results:** block Claude until a complete review can establish the outcome.
- **Fork PRs:** receive OCR feedback; automatic Claude follow-up is disabled.

The gate considers all findings in the current full PR review, including findings whose duplicate inline comments were suppressed. It validates the reviewed head and merge-base against the captured PR commits. If the PR changes during review, rerun against the new commits. Files OCR excludes before selection are outside its coverage guarantee.

The gate controls the Claude follow-up. It does not add a required branch-protection check or replace human review.

## Manual runs

Post a new PR conversation comment beginning with **`@ocr`** (for example, `@ocr` or `@ocr please review again`). The command must be followed by whitespace or the end of the comment. It runs the same pipeline, including the eligible Claude follow-up.

Only users whose current repository permission is write, maintain or admin can trigger manual OCR runs. Bot comments, unrelated comments, closed PRs and unauthorized commands do not start a review or cancel an active one. Editing an existing comment does not trigger a run.

Manual **`@claude`** requests retain their existing workflow and operate independently of the OCR gate.

## Trust and credentials

The OCR workflow uses `pull_request_target` so fork PRs can be reviewed with the repository secret. The pinned upstream action checks out the trusted base branch, fetches PR Git objects and reads the diff without checking out or executing fork code. The automatic Claude step also keeps the trusted checkout and uses Git/PR reads to inspect the requested commits; it runs only on same-repository PRs.

Do not change this workflow to check out a fork head or execute its install/build/test scripts while credentials are available. Review text is still untrusted input to the models.

Manual command authorization happens before the per-PR cancellation group. New authorized reviews cancel older runs for the same PR.

## Model, cost and version settings

Configuration lives in `.github/workflows/ocr.yml`:

- OCR action pinned to commit `b3dbcb634cbb39344e0a3c48ccb1cef3ecd51532`, CLI `1.12.2`.
- Anthropic Opus 5, adaptive thinking, high model effort; medium OCR review effort.
- Two concurrent OCR tasks, 15-minute per-task timeout, 500,000 total-token budget.
- 45-minute job timeout, including the Claude follow-up.
- Claude follow-up uses Opus 5 with a $10 CLI budget.

The OCR token limit stops further dispatch after it is exceeded; in-flight work can overshoot. It is not a hard dollar spending limit. Configure an Anthropic workspace spending limit for a billing ceiling. Both review stages consume API quota; low/medium-only PRs still receive both reviews.

The release and provider settings are explicit to make upgrades reviewable. When upgrading OCR, verify its output contract and run:

```sh
node --test .github/scripts/ocr-gate.test.cjs
actionlint .github/workflows/ocr.yml .github/workflows/claude.yml .github/workflows/ci.yml
```

Inspect the Actions log, OCR JSON artifact and sticky gate comment when a run blocks. OCR action success alone does not mean a clean or complete review.

## Upstream references

- [OCR CI integration](https://github.com/alibaba/open-code-review/blob/v1.12.2/pages/src/content/docs/en/integrations/ci.md)
- [Pinned action inputs and outputs](https://github.com/alibaba/open-code-review/blob/v1.12.2/action.yml)
- [Anthropic Opus 5](https://platform.claude.com/docs/en/models/opus-5/whats-new-opus-5)
