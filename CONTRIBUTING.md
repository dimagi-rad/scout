# Contributing to Scout

See `CLAUDE.md` for architecture, commands, and code style. This file covers one
process rule that the PR template enforces.

## The sibling-sweep rule (arch #268)

**Every bug or incident fix must sweep for sibling sites and account for each one.**

Across Scout's incident history, the single most predictive source of repeat
incidents is **"fixed where it bit"** — patching the one call site that happened
to break while identical sibling sites are left with the same latent bug. The
fix looks complete, the same class of bug recurs from a neighbour weeks later.

So before you merge a fix:

1. **Find the pattern.** What category of thing was wrong? (A missing guard, an
   unsafe call, a wrong predicate, an unbounded value…)
2. **Grep for every sibling.** Search the codebase for the same pattern, not just
   the file you touched. Write the grep down — it goes in the PR.
3. **Account for every hit.** Either fix it in this PR, or explicitly tick it off
   with a one-line reason it's safe/out of scope. Silent omission is the failure
   mode this rule exists to prevent.

The PR template has a **Sibling sweep** section for exactly this: paste the grep
and list each site with its disposition.

### Canonical example — #235

PR #227 fixed Postgres's 63-byte identifier truncation for **view names**. It was
correct, but it fixed only the site that bit. The sibling sweep for #235 grepped
for every minted Postgres identifier and found the same unguarded pattern on
schema names, read-only and dbt role names, refresh-schema names, and dbt
model/column names — any of which could collapse two tenants onto one physical
object. #235 routed them all through one helper (`apps/common/identifiers.py`)
and listed the full grep in [PR #299](https://github.com/dimagi-rad/scout/pull/299).
That is the bar: find the class, sweep it, close it.

### When it doesn't apply

Pure docs changes, mechanical refactors, and brand-new features with no
pre-existing buggy sibling don't need a sweep — delete the section from the PR.
When in doubt, do the grep anyway; it's cheap and occasionally surprising.
