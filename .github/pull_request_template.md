<!--
Thanks for the PR! Fill in the sections below. Delete any that don't apply,
EXCEPT "Sibling sweep" on a bug/incident fix — that one is required (see CONTRIBUTING.md).
-->

## What & why

<!-- What does this change do, and why? Link the issue: "Closes #123". -->

## Sibling sweep (required on bug/incident fixes)

<!--
The single most predictive source of repeat incidents is "fixed where it bit":
patching the one call site that broke while identical sibling sites stay broken
(arch #268). Before merging a fix, grep for the rest of the pattern and account
for every hit — fix it, or explicitly tick it off with a reason.

Canonical example: #235 / PR #299 — PR #227 had byte-capped *view* names only;
the sibling sweep found schema, role, refresh, and dbt names with the same
unguarded pattern and routed them all through one helper.

Delete this section only for changes that fix nothing (docs, pure refactors,
new features with no prior buggy sibling).
-->

- **Grep used:** <!-- e.g. `grep -rnE '_ro"|_dbt"|f"stg_' apps/ mcp_server/` -->
- **Sibling sites found & disposition:**
  - [ ] `path/to/site` — fixed / not applicable because …

## Testing

<!-- How did you verify this? New/updated tests, manual steps, etc. -->

## Notes for reviewers

<!-- Anything out of scope, follow-ups, risk areas, or deploy/migration concerns. -->
